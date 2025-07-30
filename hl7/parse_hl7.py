with open("hl7/sample_hl7.txt") as f:
    raw = f.read()

lines = raw.strip().split("\n")
for line in lines:
    fields = line.split("|")
    print(f"Segment: {fields[0]}")
    print(fields)
