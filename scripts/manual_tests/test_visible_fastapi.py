"""Test /virtual_lines/visible endpoint using TestClient"""
from fastapi.testclient import TestClient
from backend.api.main import app

client = TestClient(app)

print("Testing GET /virtual_lines/visible...")
try:
    response = client.get("/virtual_lines/visible")
    print(f"Status: {response.status_code}")

    if response.status_code == 200:
        data = response.json()
        print(f"Success! Got {len(data)} lines")

        virtual = [line for line in data if line['is_virtual']]
        physical = [line for line in data if not line['is_virtual']]

        print(f"\nVirtual lines: {len(virtual)}")
        for line in virtual:
            print(f"  {line['id']}: {line['name']} (физ. линии: {line['physical_line_ids']})")

        print(f"\nPhysical lines (not in rings): {len(physical)}")
        for line in physical[:5]:
            print(f"  {line['id']}: {line['name']}")
        if len(physical) > 5:
            print(f"  ... и еще {len(physical) - 5} линий")
    else:
        print(f"Error: {response.text}")
except Exception as e:
    print(f"Exception: {e}")
    import traceback
    traceback.print_exc()
