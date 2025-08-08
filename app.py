import os
from flask import Flask, request, render_template, redirect, url_for, flash
from werkzeug.utils import secure_filename
import requests
import pydicom
from dicomweb_client.api import DICOMwebClient
from requests.auth import HTTPBasicAuth
from requests import Session

# ─── CONFIG ──────────────────────────────────────────────────────────────────
class Config:
    ORTHANC_URL = "http://localhost:8042"
    ORTHANC_AUTH = ("orthanc", "orthanc")
    FHIR_URL   = "http://localhost:8080/fhir"
    FHIR_HDR   = {"Content-Type": "application/fhir+json"}

    UPLOAD_FOLDER = "uploads"
    ALLOWED_DCM   = {"dcm", "DCM"}
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

# ─── HELPERS ─────────────────────────────────────────────────────────────────
def allowed_file(filename, allowed_exts):
    return "." in filename and filename.rsplit(".", 1)[1] in allowed_exts

def parse_hl7_message(file_path):
    """Parse HL7 message and extract PatientID and clinical text"""
    try:
        with open(file_path, 'r', encoding='utf-8') as f:
            hl7_text = f.read()
        
        # Manual HL7 parsing (reliable fallback method)
        # Normalize line endings
        hl7_text = hl7_text.replace('\r\n', '\n').replace('\r', '\n')
        
        patient_id = None
        clinical_text = []
        
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
                    
            elif segment_type == 'NTE' and len(fields) > 3:
                # Notes segment - field 3 is comment
                note_text = fields[3].strip()
                if note_text:
                    clinical_text.append(note_text)
        
        # Join all clinical text
        report_text = '\n'.join(clinical_text) if clinical_text else "HL7 message processed"
        
        return patient_id, report_text
        
    except Exception as e:
        return None, f"Error parsing HL7: {str(e)}"

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
        # Expected JSON format:
        # {
        #   "patientId": "P1001",
        #   "clinicalText": "Patient diagnosis and findings...",
        #   "reports": ["Finding 1", "Finding 2"]  // optional
        # }
        
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
    """Parse patient data file (JSON, HL7, or TXT) and extract PatientID and clinical text"""
    file_ext = file_path.split('.')[-1].lower()
    
    if file_ext == 'json':
        return parse_json_patient_data(file_path)
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
                    return patient_id, report
                return None, "Empty file"
            except Exception as e:
                return None, f"Error reading text file: {str(e)}"
    else:
        return None, f"Unsupported file format: {file_ext}"

# ─── ROUTES ──────────────────────────────────────────────────────────────────
@app.route("/", methods=["GET", "POST"])
def index():
    if request.method == "POST":
        dicom_files = request.files.getlist("dicoms")
        hl7_files   = request.files.getlist("hl7data")

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
                    study_uid  = ds.get("StudyInstanceUID", None)
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

        # 2) Process Patient Data uploads and index by PatientID
        patient_data_index = {}  # pid -> report text
        for f in hl7_files:
            if f and allowed_file(f.filename, Config.ALLOWED_PATIENT_DATA):
                fname = secure_filename(f.filename)
                path = os.path.join(Config.UPLOAD_FOLDER, fname)
                f.save(path)

                # Parse patient data file to extract PatientID and clinical text
                pid, report = parse_patient_data_file(path)
                
                if not pid:
                    flash(f"Could not extract PatientID from file: {fname} - {report}", "warning")
                    continue
                
                patient_data_index[pid] = report
                flash(f"Successfully processed patient data: {fname} (Patient: {pid})", "success")

                # push to HAPI FHIR as DiagnosticReport
                dr = {
                    "resourceType": "DiagnosticReport",
                    "status": "final",
                    "category": [{
                        "coding": [{
                            "system": "http://terminology.hl7.org/CodeSystem/v2-0074",
                            "code": "LAB",
                            "display": "Laboratory"
                        }]
                    }],
                    "code": {
                        "coding": [{
                            "system": "http://loinc.org",
                            "code": "11502-2",
                            "display": "Laboratory report"
                        }]
                    },
                    "subject": {"reference": f"Patient/{pid}"},
                    "effectiveDateTime": "2025-08-07",
                    "conclusion": report
                }
                try:
                    r = requests.post(
                        f"{Config.FHIR_URL}/DiagnosticReport",
                        headers=Config.FHIR_HDR,
                        json=dr,
                        timeout=30
                    )
                    if r.status_code not in (200, 201):
                        flash(f"FHIR HL7 report failed for PID {pid} ({r.status_code}): {r.text[:100]}", "error")
                except requests.exceptions.RequestException as e:
                    flash(f"Network error uploading FHIR report for PID {pid}: {str(e)}", "error")

        # 3) Match and queue AI ingestion
        pids_with_images = set(dicom_index)
        pids_with_patient_data = set(patient_data_index)
        matched = pids_with_images & pids_with_patient_data
        for pid in matched:
            sop_list = dicom_index[pid]
            report   = patient_data_index[pid]
            # TODO: call your AI ingestion function, e.g.:
            # ai.ingest(pid, sop_list, report)
            flash(f"Queued AI for PID {pid} ({len(sop_list)} images)", "success")

        # 4) Warn on unmatched
        for pid in pids_with_images ^ pids_with_patient_data:
            flash(f"No match for PID {pid}", "warning")

        return redirect(url_for("index"))

    return render_template("index.html")

# ─── RUN ─────────────────────────────────────────────────────────────────────
if __name__ == "__main__":
    app.run(debug=True)
