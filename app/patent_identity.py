import re
import unicodedata


def patent_number_key(number):
    value = re.sub(r"\s+", "", unicodedata.normalize("NFKC", str(number or ""))).upper()
    value = re.sub(r"^(?:ZL|CN)(?=\d)", "", value)
    return re.sub(r"(?<=\d)[ABCUYS]\d?$", "", value) if re.fullmatch(r"\d{6,11}[ABCUYS]\d?", value) else value


def patent_key(name, number):
    if number := patent_number_key(number):
        return "patent:" + number
    return "title:" + re.sub(r"\W+", "", unicodedata.normalize("NFKC", name)).casefold()


def patent_lookup_keys(keys):
    result = {key: None for key in keys if key}
    for key in tuple(result):
        if re.fullmatch(r"patent:20\d{2}[12389]\d{7}\.[0-9X]", key):
            result[key.split(".")[0]] = None
    return tuple(result)
