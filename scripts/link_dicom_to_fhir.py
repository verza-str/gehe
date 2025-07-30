import requests

# Example: Create an ImagingStudy pointing to an Orthanc DICOM instance
study = {
    "resourceType": "ImagingStudy",
    "status": "available",
    "subject": {"reference": "Patient/example"},
    "series": [{
        "uid": "1.2.3.4.5",
        "instance": [{
            "uid": "1.2.3.4.5.6",
            "sopClass": "1.2.840.10008.5.1.4.1.1.2"
        }]
    }]
}

res = requests.post(
    "http://localhost:8080/fhir/ImagingStudy",
    headers={"Content-Type": "application/fhir+json"},
    json=study
)

print("FHIR ImagingStudy upload:", res.status_code)
