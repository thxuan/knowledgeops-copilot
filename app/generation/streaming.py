"""Extract display-only claim text from incomplete JSON, never final citations."""
import json


def claims_preview(raw):
    size = len(raw)
    decoder = json.JSONDecoder()

    def space(i):
        while i < size and raw[i].isspace():
            i += 1
        return i

    def string(i):
        i += 1
        chars = []
        escapes = {'"': '"', '\\': '\\', '/': '/', 'b': '\b', 'f': '\f',
                   'n': '\n', 'r': '\r', 't': '\t'}
        while i < size:
            char = raw[i]
            if char == '"':
                return "".join(chars), i + 1, True
            if char != '\\':
                chars.append(char)
                i += 1
                continue
            if i + 1 >= size:
                break
            code = raw[i + 1]
            if code in escapes:
                chars.append(escapes[code])
                i += 2
            elif code == 'u':
                if i + 6 > size:
                    break
                try:
                    number = int(raw[i + 2:i + 6], 16)
                except ValueError:
                    break
                if 0xD800 <= number <= 0xDBFF:
                    if i + 12 > size or raw[i + 6:i + 8] != '\\u':
                        break
                    try:
                        low = int(raw[i + 8:i + 12], 16)
                    except ValueError:
                        break
                    if not 0xDC00 <= low <= 0xDFFF:
                        break
                    chars.append(chr(0x10000 + ((number - 0xD800) << 10) + low - 0xDC00))
                    i += 12
                elif 0xDC00 <= number <= 0xDFFF:
                    break
                else:
                    chars.append(chr(number))
                    i += 6
            else:
                break
        return "".join(chars), i, False

    def value(i, depth=0):
        i = space(i)
        if depth > 16 or i >= size:
            return None, i, False
        if raw[i] == '"':
            return string(i)
        if raw[i] == '{':
            result, i = {}, space(i + 1)
            while i < size:
                if raw[i] == '}':
                    return result, i + 1, True
                if raw[i] != '"':
                    break
                key, i, complete = string(i)
                i = space(i)
                if not complete or i >= size or raw[i] != ':':
                    break
                child, i, complete = value(i + 1, depth + 1)
                result[key] = child
                i = space(i)
                if not complete or i >= size:
                    break
                if raw[i] == ',':
                    i = space(i + 1)
                elif raw[i] != '}':
                    break
            return result, i, False
        if raw[i] == '[':
            result, i = [], space(i + 1)
            while i < size:
                if raw[i] == ']':
                    return result, i + 1, True
                child, i, complete = value(i, depth + 1)
                result.append(child)
                i = space(i)
                if not complete or i >= size:
                    break
                if raw[i] == ',':
                    i = space(i + 1)
                elif raw[i] != ']':
                    break
            return result, i, False
        try:
            item, end = decoder.raw_decode(raw, i)
            return item, end, True
        except (ValueError, RecursionError):
            return None, size, False

    parsed, _, _ = value(0)
    if not isinstance(parsed, dict) or parsed.get("insufficient_context") is True:
        return ""
    claims = parsed.get("claims")
    if not isinstance(claims, list):
        return ""
    return "\n".join(item["text"] for item in claims
                     if isinstance(item, dict) and isinstance(item.get("text"), str))
