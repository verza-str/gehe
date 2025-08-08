import sys
sys.path.append('.')
from app import parse_patient_data_file

# Test JSON parsing
test_files = [
    "uploads/P1001_patient_data.json",
    "uploads/P1002_patient_data.json", 
    "uploads/98.12.21_patient_data.json"
]

print("🧪 Testing Patient Data File Parsing")
print("=" * 40)

for file_path in test_files:
    try:
        pid, report = parse_patient_data_file(file_path)
        if pid:
            print(f"✅ {file_path}")
            print(f"   Patient ID: {pid}")
            print(f"   Report Preview: {report[:100]}...")
        else:
            print(f"❌ {file_path}: {report}")
    except Exception as e:
        print(f"❌ {file_path}: Error - {e}")
    print()

print("✅ JSON patient data parsing test complete!")
