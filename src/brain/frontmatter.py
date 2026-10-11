"""frontmatter の最小パーサ（scripts/lib_fm.py と同じ YAML サブセット。外部依存なし）。"""


def parse_text(text):
    """frontmatter(dict) と body(str) を返す。使用するYAMLは
    scalar / list / list-of-dict のみ。"""
    if not text.startswith("---"):
        return {}, text
    end = text.find("\n---", 3)
    if end == -1:
        return {}, text
    raw, body = text[4:end], text[end + 4:]
    fm, cur_key, cur_list, cur_obj = {}, None, None, None
    for line in raw.split("\n"):
        if not line.strip() or line.strip().startswith("#"):
            continue
        indent = len(line) - len(line.lstrip())
        s = line.strip()
        if indent == 0 and ":" in s and not s.startswith("-"):
            cur_obj = None
            key, _, val = s.partition(":")
            key, val = key.strip(), val.strip()
            if val == "":
                cur_key, cur_list = key, []
                fm[key] = cur_list
            elif val.startswith("[") and val.endswith("]"):
                inner = val[1:-1].strip()
                fm[key] = [v.strip().strip("'\"") for v in inner.split(",") if v.strip()]
                cur_key, cur_list = None, None
            else:
                fm[key] = val.strip("'\"")
                cur_key, cur_list = None, None
        elif s.startswith("- ") and cur_list is not None:
            item = s[2:].strip()
            if ":" in item and not item.startswith("["):
                k, _, v = item.partition(":")
                cur_obj = {k.strip(): v.strip().strip("'\"")}
                cur_list.append(cur_obj)
            else:
                cur_list.append(item.strip("'\""))
                cur_obj = None
        elif cur_obj is not None and ":" in s:
            k, _, v = s.partition(":")
            cur_obj[k.strip()] = v.strip().strip("'\"")
    return fm, body
