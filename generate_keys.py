import random
import string

def generate_license_key():
    parts = []
    for _ in range(4):  # 4 segments
        segment = ''.join(random.choices(string.ascii_uppercase + string.digits, k=4))
        parts.append(segment)
    return "POS-" + "-".join(parts)
