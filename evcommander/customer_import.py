"""Read and write the CRM's bounded spreadsheet import format."""

from collections import Counter
from datetime import datetime, timedelta, timezone
from io import BytesIO
import json
import re
from uuid import uuid4
from xml.etree import ElementTree as ET
from xml.sax.saxutils import escape
from zipfile import BadZipFile, ZIP_DEFLATED, ZipFile

import commander
import customers

TEMPLATE = customers.CRM + "/import/template"
PREVIEW = customers.CRM + "/import/preview"
CONFIRM = customers.CRM + "/import/confirm"
MIME = "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
MAX_SIZE = 5 * 1024 * 1024
NS = "http://schemas.openxmlformats.org/spreadsheetml/2006/main"
REL = "http://schemas.openxmlformats.org/officeDocument/2006/relationships"
PKG = "http://schemas.openxmlformats.org/package/2006/relationships"
HEADERS = {"公司 / 企业名称": "company", "联系人": "contact_person", "电话": "phone", "微信号": "wechat",
           "客户状态": "crm_status", "客户等级": "grade", "所在地区": "region", "介绍渠道": "referrer",
           "商机 / 需求方向": "opportunity", "到期日": "expire_at", "标签": "tags"}


def template():
    buffer = BytesIO()
    files = {
        "[Content_Types].xml": '<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">'
        '<Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/>'
        '<Default Extension="xml" ContentType="application/xml"/>'
        '<Override PartName="/xl/workbook.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet.main+xml"/>'
        '<Override PartName="/xl/worksheets/sheet1.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.worksheet+xml"/></Types>',
        "_rels/.rels": f'<Relationships xmlns="{PKG}"><Relationship Id="rId1" Type="{REL}/officeDocument" Target="xl/workbook.xml"/></Relationships>',
        "xl/workbook.xml": f'<workbook xmlns="{NS}" xmlns:r="{REL}"><sheets><sheet name="客户" sheetId="1" r:id="rId1"/></sheets></workbook>',
        "xl/_rels/workbook.xml.rels": f'<Relationships xmlns="{PKG}"><Relationship Id="rId1" Type="{REL}/worksheet" Target="worksheets/sheet1.xml"/></Relationships>',
    }
    cells = "".join(f'<c r="{chr(65 + i)}1" t="inlineStr"><is><t>{escape(name)}</t></is></c>'
                    for i, name in enumerate(HEADERS))
    files["xl/worksheets/sheet1.xml"] = f'<worksheet xmlns="{NS}"><sheetData><row r="1">{cells}</row></sheetData></worksheet>'
    with ZipFile(buffer, "w", ZIP_DEFLATED) as archive:
        for name, data in files.items():
            archive.writestr(name, data.encode())
    return buffer.getvalue()


def xml(archive, name):
    if archive.getinfo(name).file_size > 8 * 1024 * 1024:
        raise commander.ChatError("表格内容过大。")
    raw = archive.read(name)
    if b"\x00" in raw or re.search(rb"<!\s*(DOCTYPE|ENTITY)", raw, re.I):
        raise commander.ChatError("表格含不支持的 XML 格式。")
    return ET.fromstring(raw)


def spreadsheet(raw):
    tag = f"{{{NS}}}"
    try:
        with ZipFile(BytesIO(raw)) as archive:
            sheet = xml(archive, "xl/worksheets/sheet1.xml")
            shared = []
            if "xl/sharedStrings.xml" in archive.namelist():
                shared = ["".join(cell.itertext()) for cell in xml(archive, "xl/sharedStrings.xml")]
            rows = sheet.findall(f"./{tag}sheetData/{tag}row")
            if not 2 <= len(rows) <= 501:
                raise commander.ChatError("请在模板首行下填写 1 到 500 条客户资料。")
            header = {}
            output = []
            for position, row in enumerate(rows):
                values = {}
                for cell in row:
                    reference = re.match(r"([A-Z]{1,3})[0-9]+$", cell.get("r", ""))
                    if not reference:
                        continue
                    raw_value = cell.findtext(tag + "v", "")
                    if cell.get("t") == "s":
                        raw_value = shared[int(raw_value)]
                    if cell.get("t") == "inlineStr":
                        raw_value = "".join(node.text or "" for node in cell.iter(tag + "t"))
                    values[reference[1]] = raw_value.strip()
                if position == 0:
                    header = {column: HEADERS[value] for column, value in values.items() if value in HEADERS}
                    if "company" not in header.values():
                        raise commander.ChatError("请使用客户中心下载的导入模板。")
                    continue
                mapped = {key: values[column] for column, key in header.items() if values.get(column)}
                if mapped:
                    output.append((position + 1, mapped))
            return output
    except (BadZipFile, OSError, ET.ParseError, KeyError, ValueError, IndexError, RuntimeError) as error:
        raise commander.ChatError("无法读取 Excel，请检查文件或重新下载模板。") from error


def preview(raw):
    rows = spreadsheet(raw)
    review = []
    counts = dict.fromkeys(("new", "duplicate", "invalid", "conflict"), 0)
    token = uuid4().hex
    with commander.database() as db:
        db.execute("BEGIN IMMEDIATE")
        customers.initialize(db)
        seen = set()
        for line, data in rows:
            data["tags"] = [tag.strip() for tag in re.split("[,，]", data.get("tags", "")) if tag.strip()]
            reverse = {label: key for key, label in customers.STATUSES.items()}
            data["crm_status"] = reverse.get(data.get("crm_status"), data.get("crm_status", "contact"))
            state, note = "new", ""
            try:
                checked = customers.contact_data(data, {})
                keys = {key for key in (("phone", re.sub(r"[\s()+-]", "", checked["phone"])),
                                        ("wechat", checked["wechat"].casefold())) if key[1]}
                if customers.duplicate(db, checked) or keys & seen:
                    state, note = "duplicate", "联系方式已存在"
                seen.update(keys)
            except commander.ChatError as error:
                state, note = "invalid", str(error)
            counts[state] += 1
            review.append({"row": line, "data": data, "status": state, "note": note})
        cutoff = (datetime.now(timezone.utc) - timedelta(days=1)).isoformat()
        db.execute("DELETE FROM crm_imports WHERE created_at<? AND tenant_id=?",
                   (cutoff, commander.tenant()))
        db.execute("INSERT INTO crm_imports (token,tenant_id,rows,created_at) VALUES (?,?,?,?)",
                   (token, commander.tenant(), json.dumps(review, ensure_ascii=False), customers.now()))
    return {"data": {"token": token, "rows": review, "counts": counts, "done": False}}


def confirm(body):
    token = body.get("token")
    if not isinstance(token, str) or not re.fullmatch("[a-f0-9]{32}", token):
        raise commander.ChatError("导入预览已失效，请重新上传。")
    with commander.database() as db:
        db.execute("BEGIN IMMEDIATE")
        customers.initialize(db)
        batch = db.execute("SELECT * FROM crm_imports WHERE token=? AND tenant_id=?",
                           (token, commander.tenant())).fetchone()
        cutoff = (datetime.now(timezone.utc) - timedelta(days=1)).isoformat()
        if not batch or batch["created_at"] < cutoff:
            raise commander.ChatError("导入预览已失效，请重新上传。")
        if batch["result"]:
            return {"data": json.loads(batch["result"])}
        rows = json.loads(batch["rows"])
        if any(row["status"] in {"invalid", "conflict"} for row in rows):
            raise commander.ChatError("请先修正表格中的错误。")
        counts = Counter(created=0, skipped=0)
        for row in rows:
            if row["status"] == "duplicate":
                counts["skipped"] += 1
                continue
            counts["created" if customers.save(db, row["data"], source="spreadsheet")["created"] else "skipped"] += 1
        db.execute("UPDATE crm_imports SET rows='[]',result=? WHERE token=? AND tenant_id=?",
                   (json.dumps(counts), token, commander.tenant()))
        return {"data": dict(counts)}
