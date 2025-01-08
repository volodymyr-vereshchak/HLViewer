import json

if __name__ == "__main__":
    path = "FLOWTYPE.json"
    with open(path, "r", encoding="utf8") as file:
        flow_type = json.load(file)["FLOWTYPE"]
    print(flow_type[0]["TYPENAME"].strip())
    print(len(flow_type[0]["TYPENAME"].strip()))
