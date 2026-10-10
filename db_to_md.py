import sqlite3
import json
import sys
import os

db_path = "/home/codespace/.gemini/antigravity-cli/conversations/227be089-2baa-49a7-b502-9ccb3c075777.db"
output_path = "session_report.md"

if not os.path.exists(db_path):
    print(f"Error: Database file not found at {db_path}")
    sys.exit(1)

# Helper function to safely serialize binary data types
def byte_decoder(obj):
    if isinstance(obj, bytes):
        try:
            return obj.decode('utf-8')
        except:
            return obj.hex()
    return str(obj)

conn = sqlite3.connect(db_path)
cursor = conn.cursor()

cursor.execute("SELECT name FROM sqlite_master WHERE type='table';")
tables = [t[0] for t in cursor.fetchall()]

with open(output_path, "w", encoding="utf-8") as f:
    f.write("# Antigravity Local Session Export\n")
    f.write(f"**Session ID:** `227be089-2baa-49a7-b502-9ccb3c075777`\n\n---\n\n")

    for table in tables:
        f.write(f"## Table Structure: {table}\n\n")
        try:
            cursor.execute(f"SELECT * FROM [{table}];")
            cols = [d[0] for d in cursor.description]
            rows = cursor.fetchall()
            
            for row in rows:
                # Convert everything to safe datatypes, handling raw bytes safely
                record = {}
                for k, v in zip(cols, row):
                    if isinstance(v, bytes):
                        try:
                            record[k] = v.decode('utf-8')
                        except:
                            record[k] = f"<Binary Data (Hex): {v.hex()[:60]}...>"
                    else:
                        record[k] = v
                
                # Check for readable context strings inside the data dump
                role = str(record.get("role", record.get("sender", "LOG_ENTRY"))).upper()
                content = record.get("content", record.get("text", record.get("body", "")))
                
                # Format nicely if it looks like standard turn content, otherwise fallback to clean JSON block
                if content:
                    f.write(f"### {role}\n")
                    f.write(f"{content}\n\n")
                else:
                    f.write(f"```json\n{json.dumps(record, indent=2, default=byte_decoder)}\n```\n\n")
                    
            f.write("\n---\n\n")
        except Exception as e:
            f.write(f"Failed to read table {table}: {e}\n\n")

conn.close()
print(f"Successfully processed all tables into {output_path} without hitting quota!")
