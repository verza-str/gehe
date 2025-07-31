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
    ALLOWED_TEXT  = {"txt", "csv", "json"}

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

# ─── ROUTES ──────────────────────────────────────────────────────────────────
@app.route("/", methods=["GET", "POST"])
def index():
    if request.method == "POST":
        dicom_files = request.files.getlist("dicoms")
        text_files  = request.files.getlist("textdata")

        # 1) Process DICOM uploads and index by PatientID
        dicom_index = {}  # pid -> list of SOP Instance UIDs
        for f in dicom_files:
            if f and allowed_file(f.filename, Config.ALLOWED_DCM):
                fname = secure_filename(f.filename)
                path = os.path.join(Config.UPLOAD_FOLDER, fname)
                f.save(path)

                # upload to Orthanc
                with open(path, "rb") as fp:
                    res = requests.post(
                        f"{Config.ORTHANC_URL}/instances",
                        auth=Config.ORTHANC_AUTH,
                        headers={"Content-Type": "application/dicom"},
                        data=fp
                    )
                if res.status_code != 200:
                    flash(f"Failed to upload {fname} ({res.status_code})", "error")
                    continue

                # read header to get IDs
                ds = pydicom.dcmread(path, stop_before_pixels=True)
                pid = ds.get("PatientID", "UNKNOWN")
                study_uid  = ds.get("StudyInstanceUID")
                series_uid = ds.get("SeriesInstanceUID")

                # query Orthanc for all instances in that series
                instances = dicom_client.search_for_instances(
                    study_instance_uid=study_uid,
                    series_instance_uid=series_uid
                )
                sop_uids = [inst['00080018']['Value'][0] for inst in instances]
                dicom_index.setdefault(pid, []).extend(sop_uids)

        # 2) Process text uploads and index by PatientID
        text_index = {}  # pid -> report text
        for f in text_files:
            if f and allowed_file(f.filename, Config.ALLOWED_TEXT):
                fname = secure_filename(f.filename)
                path = os.path.join(Config.UPLOAD_FOLDER, fname)
                f.save(path)

                # assume first line is PatientID, rest is the report
                with open(path, "r", encoding="utf-8") as fp:
                    lines = fp.read().splitlines()
                if not lines:
                    flash(f"Empty text file: {fname}", "warning")
                    continue

                pid    = lines[0].strip()
                report = "\n".join(lines[1:]).strip()
                text_index[pid] = report

                # push to HAPI FHIR as DiagnosticReport
                dr = {
                    "resourceType": "DiagnosticReport",
                    "status": "final",
                    "subject": {"reference": f"Patient/{pid}"},
                    "conclusion": report
                }
                r = requests.post(
                    f"{Config.FHIR_URL}/DiagnosticReport",
                    headers=Config.FHIR_HDR,
                    json=dr
                )
                if r.status_code not in (200, 201):
                    flash(f"FHIR report failed for PID {pid} ({r.status_code})", "error")

        # 3) Match and queue AI ingestion
        pids_with_images = set(dicom_index)
        pids_with_text   = set(text_index)
        matched = pids_with_images & pids_with_text
        for pid in matched:
            sop_list = dicom_index[pid]
            report   = text_index[pid]
            # TODO: call your AI ingestion function, e.g.:
            # ai.ingest(pid, sop_list, report)
            flash(f"Queued AI for PID {pid} ({len(sop_list)} images)", "success")

        # 4) Warn on unmatched
        for pid in pids_with_images ^ pids_with_text:
            flash(f"No match for PID {pid}", "warning")

        return redirect(url_for("index"))

    return render_template("index.html")

# ─── RUN ─────────────────────────────────────────────────────────────────────
if __name__ == "__main__":
    app.run(debug=True)
