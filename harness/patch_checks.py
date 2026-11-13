"""Detect edits to upstream test-support ranges inside production files."""
import re


def protected_ranges_touched(patch: str, meta: dict) -> list[str]:
    ranges = meta.get("protected_line_ranges", {})
    touched = set()
    path, old_line = None, None
    for line in patch.splitlines():
        if line.startswith("diff --git "):
            match = re.match(r"diff --git a/(\S+) b/", line)
            path, old_line = (match[1] if match else None), None
        elif line.startswith("--- a/") and old_line is None:
            path, old_line = line[6:].split("\t", 1)[0], None
        elif line in ("GIT binary patch", "deleted file mode 100644") or line.startswith("rename from "):
            if path in ranges:
                touched.add(path)
        elif line.startswith("@@ "):
            match = re.match(r"@@ -(\d+)(?:,\d+)? \+", line)
            old_line = int(match[1]) if match else None
        elif path in ranges and old_line is not None:
            if line.startswith("-"):
                if any(start <= old_line <= end for start, end in ranges[path]):
                    touched.add(path)
                old_line += 1
            elif line.startswith("+"):
                if any(start <= old_line <= end for start, end in ranges[path]):
                    touched.add(path)
            elif line.startswith(" "):
                old_line += 1
    return sorted(touched)
