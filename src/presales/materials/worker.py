import sys

from presales.materials.models import MAX_FILE_BYTES, ParseFailure
from presales.materials.parser import parse_bytes

if __name__ == "__main__":
    try:
        result = parse_bytes("input" + sys.argv[1], sys.stdin.buffer.read(MAX_FILE_BYTES + 1))
        sys.stdout.buffer.write(result.model_dump_json().encode("utf-8"))
    except ParseFailure as error:
        import json

        sys.stdout.buffer.write(json.dumps({"error": error.code}).encode())
