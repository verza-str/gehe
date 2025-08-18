import os
from flask import Flask, request, render_template, redirect, url_for, flash, jsonify
from werkzeug.utils import secure_filename
import requests
import pydicom
from dicomweb_client.api import DICOMwebClient
from requests.auth import HTTPBasicAuth
from requests import Session
import json
import uuid
from datetime import datetime
import time
import logging

# Set up logging
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

# ─── CONFIG ──────────────────────────────────────────────────────────────────
class Config:
    ORTHANC_URL = "http://localhost:8042"
    ORTHANC_AUTH = ("orthanc", "orthanc")
    FHIR_URL = "http://localhost:8080/fhir"
    FHIR_HDR = {"Content-Type": "application/fhir+json"}

    UPLOAD_FOLDER = "uploads"
    ALLOWED_DCM = {"dcm", "DCM"}
    ALLOWED_PATIENT_DATA = {"json", "hl7", "txt"}

# ─── APP SETUP ────────────────────────────────────────────────────────────────
app = Flask(__name__)
app.config.from_object(Config)
app.secret_key = "replace-this-with-a-secure-random-key"

# Ensure upload folder exists
os.makedirs(app.config["UPLOAD_FOLDER"], exist_ok=True)

# ─── DICOMWEB CLIENT ─────────────────────────────────────────────────────────
session = Session()
session.auth = HTTPBasicAuth(*app.config["ORTHANC_AUTH"])
dicom_client = DICOMwebClient(
    url=f"{app.config['ORTHANC_URL']}/dicom-web",
    session=session
)

# Global variable to track conversion status
conversion_status = {}

# ─── IMPROVED FHIR FUNCTIONS ────────────────────────────────────────────────
def validate_fhir_resource(resource):
    """Validate FHIR resource structure"""
    required_fields = {
        'Patient': ['resourceType', 'id'],
        'Observation': ['resourceType', 'status', 'code', 'subject'],
        'DiagnosticReport': ['resourceType', 'status', 'code', 'subject']
    }
    
    resource_type = resource.get('resourceType')
    if not resource_type:
        return False, "Missing resourceType"
    
    if resource_type in required_fields:
        for field in required_fields[resource_type]:
            if field not in resource:
                return False, f"Missing required field: {field}"
    
    return True, "Valid"

def upload_fhir_resource_with_retry(resource, resource_type, max_retries=3):
    """Upload a single FHIR resource with enhanced debugging and proper HTTP methods"""
    
    # Validate resource first
    is_valid, validation_msg = validate_fhir_resource(resource)
    if not is_valid:
        logger.error(f"Invalid {resource_type} resource: {validation_msg}")
        return False, f"Validation failed: {validation_msg}"
    
    logger.info(f"Validated {resource_type} resource successfully")
    
    # Log the exact JSON being sent
    resource_json = json.dumps(resource, indent=2)
    logger.info(f"Uploading {resource_type} JSON:\n{resource_json}")
    
    for attempt in range(max_retries):
        try:
            # Check if resource exists first
            resource_id = resource['id']
            check_url = f"{Config.FHIR_URL}/{resource_type}/{resource_id}"
            
            logger.info(f"Checking if {resource_type}/{resource_id} exists...")
            check_response = requests.get(check_url, headers=Config.FHIR_HDR, timeout=30)
            
            if check_response.status_code == 200:
                # Resource exists, use PUT to update
                url = check_url
                method = "PUT"
                logger.info(f"Resource exists, using PUT to update {resource_type}/{resource_id}")
            else:
                # Resource doesn't exist, use POST to create
                url = f"{Config.FHIR_URL}/{resource_type}"
                method = "POST"
                logger.info(f"Resource doesn't exist, using POST to create {resource_type}")
            
            logger.info(f"Attempting {method} to {url} (attempt {attempt + 1})")
            
            # Make the request
            if method == "PUT":
                response = requests.put(url, headers=Config.FHIR_HDR, json=resource, timeout=30)
            else:
                response = requests.post(url, headers=Config.FHIR_HDR, json=resource, timeout=30)
            
            # Log response details
            logger.info(f"FHIR {resource_type} {method} attempt {attempt + 1}: HTTP {response.status_code}")
            logger.info(f"Response headers: {dict(response.headers)}")
            logger.info(f"Response body: {response.text}")
            
            if response.status_code in (200, 201):
                logger.info(f"Successfully uploaded {resource_type} with ID: {resource['id']}")
                
                # Verify the resource was created by querying it back
                verify_url = f"{Config.FHIR_URL}/{resource_type}/{resource_id}"
                verify_response = requests.get(verify_url, headers=Config.FHIR_HDR, timeout=30)
                
                if verify_response.status_code == 200:
                    logger.info(f"Verified {resource_type}/{resource_id} exists in FHIR server")
                    
                    # For DiagnosticReport, also query by subject to confirm it's searchable
                    if resource_type == "DiagnosticReport":
                        patient_ref = resource.get('subject', {}).get('reference', '')
                        if patient_ref:
                            search_url = f"{Config.FHIR_URL}/{resource_type}?subject={patient_ref}"
                            search_response = requests.get(search_url, headers=Config.FHIR_HDR, timeout=30)
                            logger.info(f"Search by subject response: HTTP {search_response.status_code}")
                            if search_response.status_code == 200:
                                search_data = search_response.json()
                                total = search_data.get('total', 0)
                                logger.info(f"Found {total} DiagnosticReports for {patient_ref}")
                else:
                    logger.warning(f"Could not verify {resource_type}/{resource_id} after upload")
                
                return True, "Success"
            elif response.status_code == 400:
                # Bad request - parse the error for more details
                try:
                    error_response = response.json()
                    if 'issue' in error_response:
                        issues = error_response['issue']
                        error_details = []
                        for issue in issues:
                            diagnostics = issue.get('diagnostics', '')
                            severity = issue.get('severity', 'error')
                            error_details.append(f"{severity}: {diagnostics}")
                        error_detail = '; '.join(error_details)
                    else:
                        error_detail = response.text
                except:
                    error_detail = response.text if response.text else "Bad Request"
                
                logger.error(f"Bad request for {resource_type}: {error_detail}")
                return False, f"Bad request: {error_detail}"
            elif response.status_code == 422:
                # Unprocessable entity - validation error
                try:
                    error_response = response.json()
                    if 'issue' in error_response:
                        issues = error_response['issue']
                        error_details = []
                        for issue in issues:
                            diagnostics = issue.get('diagnostics', '')
                            error_details.append(diagnostics)
                        error_detail = '; '.join(error_details)
                    else:
                        error_detail = response.text
                except:
                    error_detail = response.text if response.text else "Validation Error"
                
                logger.error(f"Validation error for {resource_type}: {error_detail}")
                return False, f"Validation error: {error_detail}"
            else:
                # Other errors - retry
                error_detail = response.text if response.text else f"HTTP {response.status_code}"
                logger.warning(f"Upload failed for {resource_type} (attempt {attempt + 1}): {error_detail}")
                
                if attempt < max_retries - 1:
                    time.sleep(2 ** attempt)  # Exponential backoff
                else:
                    return False, f"Upload failed after {max_retries} attempts: {error_detail}"
                    
        except requests.exceptions.ConnectionError as e:
            logger.error(f"Connection error for {resource_type} (attempt {attempt + 1}): {str(e)}")
            if attempt < max_retries - 1:
                time.sleep(2 ** attempt)
            else:
                return False, f"Connection failed after {max_retries} attempts"
        except requests.exceptions.Timeout as e:
            logger.error(f"Timeout error for {resource_type} (attempt {attempt + 1}): {str(e)}")
            if attempt < max_retries - 1:
                time.sleep(2 ** attempt)
            else:
                return False, f"Timeout after {max_retries} attempts"
        except Exception as e:
            logger.error(f"Unexpected error for {resource_type}: {str(e)}")
            return False, f"Unexpected error: {str(e)}"
    
    return False, "Maximum retries exceeded"

def test_fhir_server_connection():
    """Test if FHIR server is accessible"""
    try:
        response = requests.get(f"{Config.FHIR_URL}/metadata", timeout=10)
        return response.status_code == 200
    except:
        return False

# ─── HL7 TO FHIR CONVERSION ─────────────────────────────────────────────────
def convert_hl7_to_fhir(hl7_file_path, patient_id, clinical_text, diagnostic_content=None):
    """Convert HL7 data to FHIR resources with comprehensive diagnostic report content including discharge summary"""
    try:
        # Clean patient ID for FHIR
        patient_fhir_id = patient_id.replace(".", "-").replace("_", "-").replace(" ", "-")
        
        # Ensure ID is valid (alphanumeric and hyphens only)
        import re
        if not re.match(r'^[A-Za-z0-9\-]+$', patient_fhir_id):
            # If invalid, create a safe ID
            patient_fhir_id = f"patient-{abs(hash(patient_id)) % 100000}"
        
        # Create stable, deterministic IDs
        observation_id = f"obs-{abs(hash(f'{patient_id}-observation')) % 100000}"
        diagnostic_report_id = f"dr-{abs(hash(f'{patient_id}-diagnostic-report')) % 100000}"
        
        # Use proper ISO 8601 datetime format
        current_time = datetime.now().strftime('%Y-%m-%dT%H:%M:%S.%fZ')
        
        logger.info(f"Creating FHIR resources for patient {patient_id}")
        logger.info(f"Patient FHIR ID: {patient_fhir_id}")
        logger.info(f"DiagnosticReport ID: {diagnostic_report_id}")
        logger.info(f"DateTime: {current_time}")
        
        # Create Patient resource
        patient_resource = {
            "resourceType": "Patient",
            "id": patient_fhir_id,
            "identifier": [
                {
                    "use": "usual",
                    "system": "http://hospital.org/patients",
                    "value": patient_id
                }
            ],
            "name": [
                {
                    "use": "official",
                    "family": "Patient",
                    "given": [patient_id]
                }
            ],
            "gender": "unknown",
            "active": True
        }
        
        # Create Observation resource (simplified)
        observation_resource = {
            "resourceType": "Observation",
            "id": observation_id,
            "status": "final",
            "category": [
                {
                    "coding": [
                        {
                            "system": "http://terminology.hl7.org/CodeSystem/observation-category",
                            "code": "survey",
                            "display": "Survey"
                        }
                    ]
                }
            ],
            "code": {
                "coding": [
                    {
                        "system": "http://loinc.org",
                        "code": "72133-2",
                        "display": "Clinical note"
                    }
                ]
            },
            "subject": {
                "reference": f"Patient/{patient_fhir_id}"
            },
            "effectiveDateTime": current_time,
            "valueString": clinical_text[:1000] if clinical_text else "No clinical text provided"
        }
        
        # Build conclusion content from HL7 OBX segments
        conclusion_parts = []
        
        # Add diagnostic content if available
        if diagnostic_content:
            # Add discharge summary first if available (highest priority)
            if diagnostic_content.get('discharge_summary'):
                conclusion_parts.append("=== DISCHARGE SUMMARY ===")
                conclusion_parts.extend(diagnostic_content['discharge_summary'])
                conclusion_parts.append("")
            
            # Add findings from OBX segments
            if diagnostic_content.get('findings'):
                conclusion_parts.append("FINDINGS:")
                conclusion_parts.extend([f"- {finding}" for finding in diagnostic_content['findings']])
                conclusion_parts.append("")
            
            # Add impressions/conclusions
            if diagnostic_content.get('impressions'):
                conclusion_parts.append("IMPRESSION:")
                conclusion_parts.extend([f"- {impression}" for impression in diagnostic_content['impressions']])
                conclusion_parts.append("")
            
            # Add recommendations
            if diagnostic_content.get('recommendations'):
                conclusion_parts.append("RECOMMENDATIONS:")
                conclusion_parts.extend([f"- {rec}" for rec in diagnostic_content['recommendations']])
                conclusion_parts.append("")
            
            # Add diagnosis codes
            if diagnostic_content.get('diagnosis_codes'):
                conclusion_parts.append("DIAGNOSES:")
                for diag in diagnostic_content['diagnosis_codes']:
                    conclusion_parts.append(f"- {diag['code']}: {diag['description']}")
                conclusion_parts.append("")
            
            # Add procedure codes
            if diagnostic_content.get('procedure_codes'):
                conclusion_parts.append("PROCEDURES:")
                for proc in diagnostic_content['procedure_codes']:
                    conclusion_parts.append(f"- {proc['code']}: {proc['description']}")
                conclusion_parts.append("")
            
            # Add general report text
            if diagnostic_content.get('report_text'):
                conclusion_parts.append("ADDITIONAL NOTES:")
                conclusion_parts.extend([f"- {text}" for text in diagnostic_content['report_text']])
        
        # Ensure we always have conclusion content
        conclusion_text = '\n'.join(conclusion_parts).strip()
        if not conclusion_text:
            conclusion_text = clinical_text if clinical_text else "No clinical content available from HL7 OBX segments"
        
        # Create FHIR-compliant DiagnosticReport resource
        diagnostic_report = {
            "resourceType": "DiagnosticReport",
            "id": diagnostic_report_id,
            "status": "final",
            "code": {
                "text": "Report"
            },
            "subject": {
                "reference": f"Patient/{patient_fhir_id}"
            },
            "effectiveDateTime": current_time,
            "issued": current_time,
            "conclusion": conclusion_text
        }
        
        # Add category based on content type
        is_discharge = diagnostic_content and diagnostic_content.get('discharge_summary')
        if is_discharge:
            diagnostic_report["category"] = [
                {
                    "coding": [
                        {
                            "system": "http://terminology.hl7.org/CodeSystem/v2-0074",
                            "code": "DS",
                            "display": "Discharge Summary"
                        }
                    ]
                }
            ]
            diagnostic_report["code"] = {
                "coding": [
                    {
                        "system": "http://loinc.org",
                        "code": "18842-5",
                        "display": "Discharge summary"
                    }
                ],
                "text": "Discharge Summary Report"
            }
        else:
            diagnostic_report["category"] = [
                {
                    "coding": [
                        {
                            "system": "http://terminology.hl7.org/CodeSystem/v2-0074",
                            "code": "LAB",
                            "display": "Laboratory"
                        }
                    ]
                }
            ]
            diagnostic_report["code"] = {
                "coding": [
                    {
                        "system": "http://loinc.org",
                        "code": "11502-2",
                        "display": "Laboratory report"
                    }
                ],
                "text": "Clinical Report"
            }
        
        logger.info(f"DiagnosticReport conclusion length: {len(conclusion_text)}")
        logger.info(f"DiagnosticReport JSON structure complete")
        
        return {
            "patient": patient_resource,
            "observation": observation_resource,
            "diagnostic_report": diagnostic_report
        }
        
    except Exception as e:
        logger.error(f"HL7 to FHIR conversion error: {str(e)}")
        raise Exception(f"HL7 to FHIR conversion failed: {str(e)}")

def upload_fhir_resources(fhir_resources, patient_id):
    """Upload FHIR resources to the FHIR server with comprehensive debugging and verification"""
    try:
        # Test server connection first
        if not test_fhir_server_connection():
            logger.error("FHIR server connection test failed")
            return {
                'patient': False,
                'observation': False,
                'diagnostic_report': False,
                'error': 'FHIR server is not accessible'
            }
        
        logger.info("FHIR server connection successful")
        results = {}
        detailed_errors = []
        
        # Upload Patient first
        logger.info(f"Uploading Patient resource for {patient_id}")
        success, message = upload_fhir_resource_with_retry(
            fhir_resources['patient'], 'Patient'
        )
        results['patient'] = success
        if not success:
            detailed_errors.append(f"Patient: {message}")
            logger.error(f"Patient upload failed: {message}")
        else:
            logger.info(f"Patient upload successful for {patient_id}")
        
        # Upload DiagnosticReport - this is the priority resource
        if results['patient']:
            logger.info(f"Uploading DiagnosticReport for {patient_id}")
            
            success, message = upload_fhir_resource_with_retry(
                fhir_resources['diagnostic_report'], 'DiagnosticReport'
            )
            results['diagnostic_report'] = success
            if not success:
                detailed_errors.append(f"DiagnosticReport: {message}")
                logger.error(f"DiagnosticReport upload failed: {message}")
            else:
                logger.info(f"DiagnosticReport upload successful for {patient_id}")
                
                # Additional verification: Query the FHIR server to confirm DiagnosticReport exists
                try:
                    dr_id = fhir_resources['diagnostic_report']['id']
                    verify_url = f"{Config.FHIR_URL}/DiagnosticReport/{dr_id}"
                    verify_response = requests.get(verify_url, headers=Config.FHIR_HDR, timeout=30)
                    
                    if verify_response.status_code == 200:
                        logger.info(f"✅ DiagnosticReport {dr_id} confirmed in FHIR server")
                        
                        # Also check total count
                        count_url = f"{Config.FHIR_URL}/DiagnosticReport"
                        count_response = requests.get(count_url, headers=Config.FHIR_HDR, timeout=30)
                        if count_response.status_code == 200:
                            count_data = count_response.json()
                            total = count_data.get('total', 0)
                            logger.info(f"Total DiagnosticReports in server: {total}")
                    else:
                        logger.warning(f"⚠️ Could not verify DiagnosticReport {dr_id} after upload")
                except Exception as ve:
                    logger.error(f"Error verifying DiagnosticReport: {str(ve)}")
        else:
            results['diagnostic_report'] = False
            detailed_errors.append("DiagnosticReport: Skipped due to Patient upload failure")
            logger.warning("DiagnosticReport skipped - Patient upload failed")
        
        # Upload Observation (lower priority)
        if results['patient']:
            logger.info(f"Uploading Observation for {patient_id}")
            success, message = upload_fhir_resource_with_retry(
                fhir_resources['observation'], 'Observation'
            )
            results['observation'] = success
            if not success:
                detailed_errors.append(f"Observation: {message}")
                logger.error(f"Observation upload failed: {message}")
            else:
                logger.info(f"Observation upload successful for {patient_id}")
        else:
            results['observation'] = False
            detailed_errors.append("Observation: Skipped due to Patient upload failure")
        
        if detailed_errors:
            results['error'] = '; '.join(detailed_errors)
        
        logger.info(f"Final upload results for {patient_id}: {results}")
        return results
        
    except Exception as e:
        logger.error(f"FHIR upload error for {patient_id}: {str(e)}")
        return {
            'patient': False,
            'observation': False,
            'diagnostic_report': False,
            'error': f"Upload process failed: {str(e)}"
        }

# ─── HELPERS ─────────────────────────────────────────────────────────────────
def allowed_file(filename, allowed_exts):
    return "." in filename and filename.rsplit(".", 1)[1] in allowed_exts

def parse_hl7_message(file_path):
    """Parse HL7 message and extract PatientID, clinical text, and diagnostic report content including discharge summary"""
    try:
        with open(file_path, 'r', encoding='utf-8') as f:
            hl7_text = f.read()
        
        # Manual HL7 parsing (reliable fallback method)
        # Normalize line endings
        hl7_text = hl7_text.replace('\r\n', '\n').replace('\r', '\n')
        
        patient_id = None
        clinical_text = []
        diagnostic_report_content = {
            'findings': [],
            'impressions': [],
            'recommendations': [],
            'procedure_codes': [],
            'diagnosis_codes': [],
            'report_text': [],
            'discharge_summary': []  # New field for discharge summary
        }
        
        lines = hl7_text.strip().split('\n')
        for line in lines:
            if not line.strip():
                continue
                
            fields = line.split('|')
            if len(fields) < 2:
                continue
                
            segment_type = fields[0].strip()
            
            if segment_type == 'PID' and len(fields) > 3:
                # PID segment - field 3 is patient ID
                raw_pid = fields[3].strip()
                # Handle composite patient ID (extract first part before ^)
                patient_id = raw_pid.split('^')[0] if '^' in raw_pid else raw_pid
                
            elif segment_type == 'OBX' and len(fields) > 5:
                # Observation segment - field 5 is observation value
                obs_value = fields[5].strip()
                if obs_value:
                    clinical_text.append(obs_value)
                    
                    # Check observation type in field 3 (observation identifier)
                    if len(fields) > 3:
                        obs_id = fields[3].strip().upper()
                        
                        # Check for discharge summary content
                        if any(keyword in obs_id for keyword in ['DISCHARGE', 'DSCH', 'SUMMARY']):
                            diagnostic_report_content['discharge_summary'].append(obs_value)
                        elif 'FINDING' in obs_id or 'RESULT' in obs_id:
                            diagnostic_report_content['findings'].append(obs_value)
                        elif 'IMPRESSION' in obs_id or 'CONCLUSION' in obs_id:
                            diagnostic_report_content['impressions'].append(obs_value)
                        elif 'RECOMMENDATION' in obs_id or 'SUGGEST' in obs_id:
                            diagnostic_report_content['recommendations'].append(obs_value)
                        else:
                            diagnostic_report_content['report_text'].append(obs_value)
                    
            elif segment_type == 'NTE' and len(fields) > 3:
                # Notes segment - field 3 is comment
                note_text = fields[3].strip()
                if note_text:
                    clinical_text.append(note_text)
                    
                    # Check if this note contains discharge summary information
                    note_upper = note_text.upper()
                    if any(keyword in note_upper for keyword in ['DISCHARGE SUMMARY', 'DISCHARGE:', 'SUMMARY:']):
                        diagnostic_report_content['discharge_summary'].append(note_text)
                    else:
                        diagnostic_report_content['report_text'].append(note_text)
                    
            elif segment_type == 'DG1' and len(fields) > 3:
                # Diagnosis segment - field 3 is diagnosis code, field 4 is description
                if len(fields) > 3:
                    diag_code = fields[3].strip()
                    diag_desc = fields[4].strip() if len(fields) > 4 else ''
                    if diag_code:
                        diagnostic_report_content['diagnosis_codes'].append({
                            'code': diag_code,
                            'description': diag_desc
                        })
                        clinical_text.append(f"Diagnosis: {diag_code} - {diag_desc}")
                        
            elif segment_type == 'PR1' and len(fields) > 3:
                # Procedure segment - field 3 is procedure code, field 4 is description
                if len(fields) > 3:
                    proc_code = fields[3].strip()
                    proc_desc = fields[4].strip() if len(fields) > 4 else ''
                    if proc_code:
                        diagnostic_report_content['procedure_codes'].append({
                            'code': proc_code,
                            'description': proc_desc
                        })
                        clinical_text.append(f"Procedure: {proc_code} - {proc_desc}")
        
        # Join all clinical text
        report_text = '\n'.join(clinical_text) if clinical_text else "HL7 message processed"
        
        return patient_id, report_text, diagnostic_report_content
        
    except Exception as e:
        return None, f"Error parsing HL7: {str(e)}", None

def parse_json_patient_data(file_path):
    """Parse JSON file with patient data and extract PatientID and clinical text"""
    try:
        import json
        with open(file_path, 'r', encoding='utf-8') as f:
            data = json.load(f)
        
        # Handle FHIR Patient resource format
        if data.get("resourceType") == "Patient":
            patient_id = data.get("id")
            name_parts = []
            if "name" in data and len(data["name"]) > 0:
                name_obj = data["name"][0]
                if "given" in name_obj:
                    name_parts.extend(name_obj["given"])
                if "family" in name_obj:
                    name_parts.append(name_obj["family"])
            
            patient_name = " ".join(name_parts) if name_parts else "Unknown"
            report_text = f"FHIR Patient Resource - Name: {patient_name}, Gender: {data.get('gender', 'Unknown')}, Birth Date: {data.get('birthDate', 'Unknown')}"
            
            return patient_id, report_text
        
        # Handle custom patient data format
        patient_id = data.get("patientId") or data.get("PatientID") or data.get("patient_id") or data.get("id")
        
        # Get clinical text from various possible fields
        clinical_text = []
        
        if "clinicalText" in data:
            clinical_text.append(data["clinicalText"])
        if "clinical_text" in data:
            clinical_text.append(data["clinical_text"])
        if "diagnosis" in data:
            clinical_text.append(f"Diagnosis: {data['diagnosis']}")
        if "findings" in data:
            if isinstance(data["findings"], list):
                clinical_text.extend([f"Finding: {finding}" for finding in data["findings"]])
            else:
                clinical_text.append(f"Findings: {data['findings']}")
        if "reports" in data and isinstance(data["reports"], list):
            clinical_text.extend(data["reports"])
        if "conclusion" in data:
            clinical_text.append(f"Conclusion: {data['conclusion']}")
        if "recommendations" in data:
            clinical_text.append(f"Recommendations: {data['recommendations']}")
            
        report_text = '\n'.join(clinical_text) if clinical_text else "JSON patient data processed"
        
        return patient_id, report_text
        
    except json.JSONDecodeError as e:
        return None, f"Invalid JSON format: {str(e)}"
    except Exception as e:
        return None, f"Error parsing JSON: {str(e)}"

def parse_patient_data_file(file_path):
    """Parse patient data file (JSON, HL7, or TXT) and extract PatientID, clinical text, and diagnostic content"""
    file_ext = file_path.split('.')[-1].lower()
    
    if file_ext == 'json':
        pid, report = parse_json_patient_data(file_path)
        return pid, report, None  # JSON doesn't have structured diagnostic content yet
    elif file_ext in ['hl7', 'txt']:
        # Try HL7 first, fallback to simple text format
        try:
            return parse_hl7_message(file_path)
        except:
            # Fallback to simple text format (first line = PatientID)
            try:
                with open(file_path, 'r', encoding='utf-8') as f:
                    lines = f.read().splitlines()
                if lines:
                    patient_id = lines[0].strip()
                    report = '\n'.join(lines[1:]).strip() if len(lines) > 1 else "Text data processed"
                    return patient_id, report, None
                return None, "Empty file", None
            except Exception as e:
                return None, f"Error reading text file: {str(e)}", None
    else:
        return None, f"Unsupported file format: {file_ext}", None

# ─── ROUTES ──────────────────────────────────────────────────────────────────
@app.route("/", methods=["GET", "POST"])
def index():
    if request.method == "POST":
        # Test FHIR server connection at start
        if not test_fhir_server_connection():
            flash("FHIR server is not accessible. Please check the server status.", "error")
            return render_template("index.html")
        
        dicom_files = request.files.getlist("dicoms")
        hl7_files = request.files.getlist("hl7data")

        # 1) Process DICOM uploads and index by PatientID
        dicom_index = {}  # pid -> list of SOP Instance UIDs
        for f in dicom_files:
            if f and allowed_file(f.filename, Config.ALLOWED_DCM):
                fname = secure_filename(f.filename)
                path = os.path.join(Config.UPLOAD_FOLDER, fname)
                f.save(path)

                # upload to Orthanc
                try:
                    with open(path, "rb") as fp:
                        res = requests.post(
                            f"{Config.ORTHANC_URL}/instances",
                            auth=Config.ORTHANC_AUTH,
                            headers={"Content-Type": "application/dicom"},
                            data=fp.read(),
                            timeout=30
                        )
                    if res.status_code not in (200, 201):
                        flash(f"Failed to upload {fname} ({res.status_code}): {res.text[:100]}", "error")
                        continue
                except requests.exceptions.RequestException as e:
                    flash(f"Network error uploading {fname}: {str(e)}", "error")
                    continue

                # read header to get IDs
                try:
                    ds = pydicom.dcmread(path, stop_before_pixels=True)
                    pid = ds.get("PatientID", None)
                    study_uid = ds.get("StudyInstanceUID", None)
                    series_uid = ds.get("SeriesInstanceUID", None)
                    sop_uid = ds.get("SOPInstanceUID", None)
                    
                    # Validate required DICOM fields
                    if not pid:
                        flash(f"DICOM file {fname} missing PatientID", "warning")
                        continue
                    if not sop_uid:
                        flash(f"DICOM file {fname} missing SOPInstanceUID", "warning")
                        continue

                    # For now, just use the SOP Instance UID from the file directly
                    # instead of querying DICOMweb (which might be causing issues)
                    dicom_index.setdefault(pid, []).append(sop_uid)
                    flash(f"Successfully processed DICOM: {fname} (Patient: {pid})", "success")
                        
                except Exception as e:
                    flash(f"Error reading DICOM file {fname}: {str(e)}", "error")
                    continue

        # 2) Process Patient Data uploads with improved HL7 to FHIR conversion
        patient_data_index = {}
        conversion_results = []
        
        for f in hl7_files:
            if f and allowed_file(f.filename, Config.ALLOWED_PATIENT_DATA):
                fname = secure_filename(f.filename)
                path = os.path.join(Config.UPLOAD_FOLDER, fname)
                f.save(path)

                # Parse patient data file to extract PatientID, clinical text, and diagnostic content
                result = parse_patient_data_file(path)
                if len(result) == 3:
                    pid, report, diagnostic_content = result
                else:
                    pid, report = result
                    diagnostic_content = None
                
                if not pid:
                    flash(f"Could not extract PatientID from file: {fname} - {report}", "warning")
                    continue
                
                patient_data_index[pid] = report
                
                # Create conversion ID for tracking
                conversion_id = str(uuid.uuid4())
                conversion_status[conversion_id] = {
                    "status": "processing",
                    "patient_id": pid,
                    "filename": fname,
                    "message": "Converting HL7 to FHIR..."
                }
                
                try:
                    # Convert HL7 to FHIR with diagnostic content
                    conversion_status[conversion_id]["message"] = "Creating FHIR resources with diagnostic content..."
                    fhir_resources = convert_hl7_to_fhir(path, pid, report, diagnostic_content)
                    
                    # Upload to FHIR server
                    conversion_status[conversion_id]["message"] = "Uploading to FHIR server..."
                    upload_results = upload_fhir_resources(fhir_resources, pid)
                    
                    # Check results and provide detailed feedback
                    successful_uploads = [k for k, v in upload_results.items() if k != 'error' and v]
                    failed_uploads = [k for k, v in upload_results.items() if k != 'error' and not v]
                    
                    # Consider success if Patient and DiagnosticReport are uploaded (prioritize these)
                    critical_success = upload_results.get('patient', False) and upload_results.get('diagnostic_report', False)
                    
                    if critical_success and len(failed_uploads) == 0:  # All succeeded
                        conversion_status[conversion_id] = {
                            "status": "success",
                            "patient_id": pid,
                            "filename": fname,
                            "message": f"Successfully converted and uploaded all FHIR resources with diagnostic summary for Patient {pid}"
                        }
                        flash(f"✅ Complete HL7 to FHIR conversion with diagnostic summary successful for Patient {pid}", "success")
                    elif critical_success:  # Patient and DiagnosticReport succeeded (most important)
                        error_detail = upload_results.get('error', 'Unknown error')
                        conversion_status[conversion_id] = {
                            "status": "success",
                            "patient_id": pid,
                            "filename": fname,
                            "message": f"Successfully uploaded Patient and DiagnosticReport with summary for Patient {pid}. Some optional resources failed: {', '.join(failed_uploads)}"
                        }
                        flash(f"✅ HL7 to FHIR conversion with diagnostic summary successful for Patient {pid}. Diagnostic report uploaded successfully!", "success")
                    elif len(successful_uploads) > 0:  # Partial success
                        error_detail = upload_results.get('error', 'Unknown error')
                        conversion_status[conversion_id] = {
                            "status": "partial",
                            "patient_id": pid,
                            "filename": fname,
                            "message": f"Partial success for Patient {pid}. Uploaded: {', '.join(successful_uploads)}. Failed: {', '.join(failed_uploads)}. Error: {error_detail}"
                        }
                        flash(f"⚠️ Partial HL7 to FHIR conversion for Patient {pid}. Uploaded: {', '.join(successful_uploads)}. Failed: {', '.join(failed_uploads)}. Details: {error_detail}", "warning")
                    else:  # All failed
                        error_detail = upload_results.get('error', 'All uploads failed')
                        conversion_status[conversion_id] = {
                            "status": "error",
                            "patient_id": pid,
                            "filename": fname,
                            "message": f"All uploads failed for Patient {pid}. Error: {error_detail}"
                        }
                        flash(f"❌ HL7 to FHIR conversion failed for Patient {pid}. Error: {error_detail}", "error")
                    
                    conversion_results.append({
                        "conversion_id": conversion_id,
                        "patient_id": pid,
                        "filename": fname
                    })
                    
                except Exception as e:
                    error_msg = str(e)
                    conversion_status[conversion_id] = {
                        "status": "error",
                        "patient_id": pid,
                        "filename": fname,
                        "message": f"Conversion failed: {error_msg}"
                    }
                    flash(f"❌ HL7 to FHIR conversion failed for {fname}: {error_msg}", "error")
                    logger.error(f"Conversion error for {fname}: {error_msg}")

        # 3) Match and queue AI ingestion
        pids_with_images = set(dicom_index)
        pids_with_patient_data = set(patient_data_index)
        matched = pids_with_images & pids_with_patient_data
        for pid in matched:
            sop_list = dicom_index[pid]
            report = patient_data_index[pid]
            # TODO: call your AI ingestion function, e.g.:
            # ai.ingest(pid, sop_list, report)
            flash(f"Queued AI for PID {pid} ({len(sop_list)} images)", "success")

        # 4) Warn on unmatched
        for pid in pids_with_images ^ pids_with_patient_data:
            flash(f"No match for PID {pid}", "warning")

        # Store conversion results in session or pass to template
        if conversion_results:
            return render_template("index.html", conversion_results=conversion_results)

        return redirect(url_for("index"))

    return render_template("index.html")

# ─── NEW ROUTE FOR CONVERSION STATUS ───────────────────────────────────────
@app.route("/conversion-status/<conversion_id>")
def get_conversion_status(conversion_id):
    """Get the status of a conversion process"""
    status = conversion_status.get(conversion_id, {"status": "not_found"})
    return jsonify(status)

# ─── NEW ROUTES FOR VIEWING CONVERTED FHIR RESOURCES ──────────────────────
@app.route("/fhir-resources")
def view_fhir_resources():
    """View all FHIR resources in the server"""
    try:
        if not test_fhir_server_connection():
            flash("FHIR server is not accessible", "error")
            return render_template("fhir_resources.html", resources=None)
        
        # Get all Patients
        patients_response = requests.get(f"{Config.FHIR_URL}/Patient", timeout=30)
        patients = []
        if patients_response.status_code == 200:
            patients_data = patients_response.json()
            if 'entry' in patients_data:
                patients = [entry['resource'] for entry in patients_data['entry']]
        
        # Get all Observations
        observations_response = requests.get(f"{Config.FHIR_URL}/Observation", timeout=30)
        observations = []
        if observations_response.status_code == 200:
            observations_data = observations_response.json()
            if 'entry' in observations_data:
                observations = [entry['resource'] for entry in observations_data['entry']]
        
        # Get all DiagnosticReports
        reports_response = requests.get(f"{Config.FHIR_URL}/DiagnosticReport", timeout=30)
        reports = []
        if reports_response.status_code == 200:
            reports_data = reports_response.json()
            if 'entry' in reports_data:
                reports = [entry['resource'] for entry in reports_data['entry']]
        
        resources = {
            'patients': patients,
            'observations': observations,
            'diagnostic_reports': reports
        }
        
        return render_template("fhir_resources.html", resources=resources)
        
    except Exception as e:
        logger.error(f"Error fetching FHIR resources: {str(e)}")
        flash(f"Error fetching FHIR resources: {str(e)}", "error")
        return render_template("fhir_resources.html", resources=None)

@app.route("/fhir-resource/<resource_type>/<resource_id>")
def view_single_fhir_resource(resource_type, resource_id):
    """View a single FHIR resource"""
    try:
        if not test_fhir_server_connection():
            flash("FHIR server is not accessible", "error")
            return redirect(url_for("view_fhir_resources"))
        
        response = requests.get(f"{Config.FHIR_URL}/{resource_type}/{resource_id}", timeout=30)
        
        if response.status_code == 200:
            resource = response.json()
            return render_template("single_fhir_resource.html", 
                                 resource=resource, 
                                 resource_type=resource_type,
                                 resource_id=resource_id)
        else:
            flash(f"Resource not found: {resource_type}/{resource_id}", "error")
            return redirect(url_for("view_fhir_resources"))
            
    except Exception as e:
        logger.error(f"Error fetching FHIR resource: {str(e)}")
        flash(f"Error fetching FHIR resource: {str(e)}", "error")
        return redirect(url_for("view_fhir_resources"))

@app.route("/conversion-history")
def conversion_history():
    """View conversion history"""
    return render_template("conversion_history.html", conversions=conversion_status)

# ─── RUN ─────────────────────────────────────────────────────────────────────
if __name__ == "__main__":
    app.run(debug=True)
