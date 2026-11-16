import hashlib
import re


def public_source(name):
    if re.fullmatch(r'[\w.:-]{1,128}',name) and '://' not in name:
        return name
    return 'source-sha256-'+hashlib.sha256(name.encode('utf-8')).hexdigest()
