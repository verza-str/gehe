import requests

# Replace with your actual DICOM file
with open("dicom/sample/0015.DCM", "rb") as f:
    response = requests.post(
        "http://localhost:8042/instances",
        auth=("orthanc", "orthanc"),
        headers={"Content-Type": "application/dicom"},
        data=f
    )

print("Upload status:", response.status_code)
print("Response:", response.json())
