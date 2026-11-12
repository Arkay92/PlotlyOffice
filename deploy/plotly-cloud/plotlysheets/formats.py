"""Workbook validation plus XLSX, ODS, CSV, TSV and JSON interchange."""
import csv
import io
import json
import re
from copy import deepcopy
from pathlib import Path
from uuid import uuid4

MAX_BYTES = 25 * 1024 * 1024
MAX_SHEETS = 50
MAX_CELLS = 250_000
CELL_RE = re.compile(r"^[A-Z]{1,3}[1-9][0-9]{0,5}$")


def blank_workbook(name="Sheet1"):
    sheet_id = uuid4().hex[:12]
    return {
        "schema": 1,
        "active": sheet_id,
        "order": [sheet_id],
        "sheets": {
            sheet_id: {
                "name": name[:80] or "Sheet1",
                "rowCount": 100,
                "colCount": 26,
                "frozenRows": 0,
                "frozenCols": 0,
                "cells": {},
                "merges": [],
                "colWidths": {},
                "rowHeights": {},
            }
        },
    }


def checked(value):
    if value is None:
        return blank_workbook()
    if not isinstance(value, dict) or value.get("schema") != 1:
        raise ValueError("This is not a supported PlotlySheets workbook.")
    order, sheets = value.get("order"), value.get("sheets")
    if not isinstance(order, list) or not isinstance(sheets, dict) or not 1 <= len(order) <= MAX_SHEETS:
        raise ValueError(f"A workbook must contain 1 to {MAX_SHEETS} sheets.")
    if len(set(order)) != len(order) or set(order) != set(sheets):
        raise ValueError("The sheet order is invalid.")
    if value.get("active") not in sheets:
        raise ValueError("The active sheet is invalid.")
    total = 0
    clean = {"schema": 1, "active": value["active"], "order": list(order), "sheets": {}}
    names = set()
    for sheet_id in order:
        sheet = sheets[sheet_id]
        if not isinstance(sheet, dict):
            raise ValueError("Invalid worksheet data.")
        name = " ".join(str(sheet.get("name", "Sheet")).split())[:80] or "Sheet"
        if name.lower() in names:
            raise ValueError("Worksheet names must be unique.")
        names.add(name.lower())
        rows, cols = sheet.get("rowCount", 100), sheet.get("colCount", 26)
        if not isinstance(rows, int) or not 1 <= rows <= 10000 or not isinstance(cols, int) or not 1 <= cols <= 702:
            raise ValueError("Worksheet size exceeds the 10,000 row by 702 column limit.")
        cells = sheet.get("cells", {})
        if not isinstance(cells, dict):
            raise ValueError("Worksheet cells are invalid.")
        total += len(cells)
        if total > MAX_CELLS:
            raise ValueError(f"A workbook can contain up to {MAX_CELLS:,} non-empty cells.")
        cell_clean = {}
        for address, cell in cells.items():
            if not isinstance(address, str) or not CELL_RE.fullmatch(address) or not isinstance(cell, dict):
                raise ValueError("A cell address or value is invalid.")
            raw = cell.get("v", "")
            if not isinstance(raw, (str, int, float, bool)) or isinstance(raw, str) and len(raw) > 50000:
                raise ValueError(f"Cell {address} is too large or invalid.")
            item = {"v": raw}
            style = cell.get("s", {})
            if isinstance(style, dict):
                allowed = {"bold", "italic", "underline", "align", "fill", "color", "format", "wrap"}
                item["s"] = {str(k): v for k, v in style.items() if k in allowed and isinstance(v, (str, bool, int, float))}
            cell_clean[address] = item
        clean["sheets"][sheet_id] = {
            "name": name, "rowCount": rows, "colCount": cols,
            "frozenRows": max(0, min(rows, int(sheet.get("frozenRows", 0) or 0))),
            "frozenCols": max(0, min(cols, int(sheet.get("frozenCols", 0) or 0))),
            "cells": cell_clean,
            "merges": [str(x)[:50] for x in sheet.get("merges", []) if isinstance(x, str)][:1000],
            "colWidths": {str(k): max(40, min(600, int(v))) for k, v in sheet.get("colWidths", {}).items() if str(k).isdigit()},
            "rowHeights": {str(k): max(20, min(300, int(v))) for k, v in sheet.get("rowHeights", {}).items() if str(k).isdigit()},
        }
    return clean


def col_name(index):
    result = ""
    while index:
        index, rem = divmod(index - 1, 26)
        result = chr(65 + rem) + result
    return result


def cell_value(cell):
    return cell.get("v", "") if isinstance(cell, dict) else ""


def delimited_import(data, delimiter, name):
    try:
        text = data.decode("utf-8-sig")
    except UnicodeDecodeError:
        text = data.decode("cp1252")
    rows = list(csv.reader(io.StringIO(text), delimiter=delimiter))
    if len(rows) > 10000 or max((len(r) for r in rows), default=0) > 702:
        raise ValueError("The delimited file exceeds worksheet limits.")
    book = blank_workbook(name)
    sheet = book["sheets"][book["active"]]
    sheet["rowCount"] = max(100, len(rows))
    sheet["colCount"] = max(26, max((len(r) for r in rows), default=0))
    for r, row in enumerate(rows, 1):
        for c, value in enumerate(row, 1):
            if value:
                sheet["cells"][f"{col_name(c)}{r}"] = {"v": value, "s": {}}
    return book, []


def import_xlsx(data):
    from openpyxl import load_workbook
    from openpyxl.styles import PatternFill
    try:
        source = load_workbook(io.BytesIO(data), data_only=False, read_only=False)
    except Exception as exc:
        raise ValueError("This Excel workbook is damaged, encrypted or unsupported.") from exc
    book = blank_workbook()
    book["order"], book["sheets"] = [], {}
    warnings = []
    for ws in source.worksheets[:MAX_SHEETS]:
        sid = uuid4().hex[:12]
        book["order"].append(sid)
        sheet = {
            "name": ws.title[:80], "rowCount": max(100, min(10000, ws.max_row)),
            "colCount": max(26, min(702, ws.max_column)), "frozenRows": 0, "frozenCols": 0,
            "cells": {}, "merges": [str(x) for x in list(ws.merged_cells.ranges)[:1000]],
            "colWidths": {}, "rowHeights": {},
        }
        if ws.freeze_panes:
            sheet["frozenRows"] = max(0, ws.freeze_panes.row - 1)
            sheet["frozenCols"] = max(0, ws.freeze_panes.column - 1)
        for key, dim in ws.column_dimensions.items():
            if dim.width:
                from openpyxl.utils.cell import column_index_from_string
                sheet["colWidths"][str(column_index_from_string(key))] = int(dim.width * 7 + 12)
        for idx, dim in ws.row_dimensions.items():
            if dim.height:
                sheet["rowHeights"][str(idx)] = int(dim.height * 1.34)
        for row in ws.iter_rows():
            for cell in row:
                if cell.value is None:
                    continue
                style = {}
                if cell.font.bold: style["bold"] = True
                if cell.font.italic: style["italic"] = True
                if cell.font.underline: style["underline"] = True
                if cell.font.color and cell.font.color.type == "rgb": style["color"] = "#" + cell.font.color.rgb[-6:]
                if isinstance(cell.fill, PatternFill) and cell.fill.fill_type and cell.fill.fgColor.type == "rgb":
                    style["fill"] = "#" + cell.fill.fgColor.rgb[-6:]
                if cell.alignment.horizontal: style["align"] = cell.alignment.horizontal
                if cell.alignment.wrap_text: style["wrap"] = True
                if cell.number_format and cell.number_format != "General": style["format"] = cell.number_format
                sheet["cells"][cell.coordinate] = {"v": cell.value, "s": style}
        book["sheets"][sid] = sheet
    if len(source.worksheets) > MAX_SHEETS:
        warnings.append(f"Imported the first {MAX_SHEETS} worksheets.")
    book["active"] = book["order"][min(source.index(source.active), len(book["order"]) - 1)]
    warnings.append("Macros, external data connections, pivot tables, conditional formatting and advanced drawings are not imported.")
    return checked(book), warnings


def import_xls(data):
    import xlrd
    try:
        source = xlrd.open_workbook(file_contents=data)
    except Exception as exc:
        raise ValueError("This legacy Excel workbook is damaged, encrypted or unsupported.") from exc
    book = blank_workbook(); book["order"], book["sheets"] = [], {}
    for ws in source.sheets()[:MAX_SHEETS]:
        sid = uuid4().hex[:12]; book["order"].append(sid)
        sheet = {"name": ws.name[:80], "rowCount": max(100, min(10000, ws.nrows)),
                 "colCount": max(26, min(702, ws.ncols)), "frozenRows": 0, "frozenCols": 0,
                 "cells": {}, "merges": [], "colWidths": {}, "rowHeights": {}}
        for r in range(min(ws.nrows, 10000)):
            for c in range(min(ws.ncols, 702)):
                item = ws.cell(r, c)
                if item.ctype in (xlrd.XL_CELL_EMPTY, xlrd.XL_CELL_BLANK): continue
                value = item.value
                if item.ctype == xlrd.XL_CELL_BOOLEAN: value = bool(value)
                if item.ctype == xlrd.XL_CELL_NUMBER and float(value).is_integer(): value = int(value)
                if item.ctype == xlrd.XL_CELL_DATE:
                    value = xlrd.xldate_as_datetime(value, source.datemode).isoformat(sep=" ")
                sheet["cells"][f"{col_name(c + 1)}{r + 1}"] = {"v": value, "s": {}}
        book["sheets"][sid] = sheet
    book["active"] = book["order"][0]
    return checked(book), ["Legacy XLS formulas are imported as their last calculated values. Common formatting may be simplified."]


def import_ods(data):
    from odf import teletype
    from odf.opendocument import load
    from odf.table import Table, TableCell, TableRow
    try:
        source = load(io.BytesIO(data))
    except Exception as exc:
        raise ValueError("This OpenDocument spreadsheet is damaged or unsupported.") from exc
    book = blank_workbook(); book["order"], book["sheets"] = [], {}
    for table in source.spreadsheet.getElementsByType(Table)[:MAX_SHEETS]:
        sid = uuid4().hex[:12]; book["order"].append(sid)
        sheet = {"name": str(table.getAttribute("name") or "Sheet")[:80], "rowCount": 100, "colCount": 26,
                 "frozenRows": 0, "frozenCols": 0, "cells": {}, "merges": [], "colWidths": {}, "rowHeights": {}}
        row_number = 0
        for row in table.getElementsByType(TableRow):
            row_repeat = min(10000 - row_number, int(row.getAttribute("numberrowsrepeated") or 1))
            cells, col_number = [], 0
            for node in row.childNodes:
                if getattr(node, "qname", (None, None))[1] != "table-cell": continue
                repeat = min(702 - col_number, int(node.getAttribute("numbercolumnsrepeated") or 1))
                formula = node.getAttribute("formula")
                value = teletype.extractText(node)
                if formula:
                    value = re.sub(r"\[\.([A-Z]+\d+):\.([A-Z]+\d+)\]", r"\1:\2", str(formula).removeprefix("of:="))
                    value = re.sub(r"\[\.([A-Z]+\d+)\]", r"\1", value)
                    value = "=" + value
                elif node.getAttribute("valuetype") in ("float", "currency", "percentage"):
                    try: value = float(node.getAttribute("value"))
                    except (TypeError, ValueError): pass
                elif node.getAttribute("valuetype") == "boolean": value = node.getAttribute("booleanvalue") == "true"
                cells.extend([value] * repeat); col_number += repeat
            for offset in range(row_repeat):
                row_number += 1
                if row_number > 10000: break
                for c, value in enumerate(cells[:702], 1):
                    if value not in ("", None): sheet["cells"][f"{col_name(c)}{row_number}"] = {"v": value, "s": {}}
        sheet["rowCount"] = max(100, row_number); sheet["colCount"] = max(26, max((parse_col(a) for a in sheet["cells"]), default=1))
        book["sheets"][sid] = sheet
    if not book["order"]: raise ValueError("The ODS file does not contain a worksheet.")
    book["active"] = book["order"][0]
    return checked(book), ["Common ODS values and formulas were imported. Advanced styles, charts and database ranges may be simplified."]


def parse_col(address):
    value = 0
    for ch in re.match(r"[A-Z]+", address).group(): value = value * 26 + ord(ch) - 64
    return value


def export_xlsx(book, title):
    from openpyxl import Workbook
    from openpyxl.styles import Alignment, Font, PatternFill
    from openpyxl.utils.cell import get_column_letter
    book = checked(book)
    target = Workbook()
    target.remove(target.active)
    for sid in book["order"]:
        source = book["sheets"][sid]
        ws = target.create_sheet(source["name"][:31])
        for address, item in source["cells"].items():
            cell = ws[address]
            value, style = item.get("v", ""), item.get("s", {})
            cell.value = value
            cell.font = Font(bold=bool(style.get("bold")), italic=bool(style.get("italic")),
                             underline="single" if style.get("underline") else None,
                             color=str(style.get("color", "#000000")).lstrip("#"))
            fill = str(style.get("fill", ""))
            if re.fullmatch(r"#[0-9a-fA-F]{6}", fill):
                cell.fill = PatternFill("solid", fgColor=fill[1:])
            cell.alignment = Alignment(horizontal=style.get("align"), wrap_text=bool(style.get("wrap")))
            if style.get("format"): cell.number_format = str(style["format"])[:100]
        for merge in source["merges"]:
            try: ws.merge_cells(merge)
            except ValueError: pass
        for idx, width in source["colWidths"].items():
            ws.column_dimensions[get_column_letter(int(idx))].width = max(3, (width - 12) / 7)
        for idx, height in source["rowHeights"].items():
            ws.row_dimensions[int(idx)].height = height / 1.34
        if source["frozenRows"] or source["frozenCols"]:
            ws.freeze_panes = f"{col_name(source['frozenCols'] + 1)}{source['frozenRows'] + 1}"
    target.active = book["order"].index(book["active"])
    target.properties.title = title
    stream = io.BytesIO(); target.save(stream)
    return stream.getvalue(), "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"


def export_xls(book, title):
    import xlwt
    book = checked(book); target = xlwt.Workbook()
    for sid in book["order"]:
        source = book["sheets"][sid]; ws = target.add_sheet(source["name"][:31])
        for address, item in source["cells"].items():
            match = re.fullmatch(r"([A-Z]+)(\d+)", address); row, col = int(match.group(2)) - 1, colIndex(match.group(1)) - 1
            value = item.get("v", "")
            try: ws.write(row, col, xlwt.Formula(value[1:]) if isinstance(value, str) and value.startswith("=") else value)
            except Exception: ws.write(row, col, str(value))
    stream = io.BytesIO(); target.save(stream)
    return stream.getvalue(), "application/vnd.ms-excel"


def colIndex(name):
    value = 0
    for ch in name: value = value * 26 + ord(ch) - 64
    return value


def export_ods(book, title):
    from odf.opendocument import OpenDocumentSpreadsheet
    from odf.table import Table, TableCell, TableRow
    from odf.text import P
    book = checked(book); target = OpenDocumentSpreadsheet()
    for sid in book["order"]:
        source = book["sheets"][sid]; table = Table(name=source["name"])
        max_row = max((int(re.search(r"\d+", a).group()) for a in source["cells"]), default=1)
        max_col = max((parse_col(a) for a in source["cells"]), default=1)
        for r in range(1, max_row + 1):
            row = TableRow()
            for c in range(1, max_col + 1):
                value = cell_value(source["cells"].get(f"{col_name(c)}{r}")); kwargs = {}
                if isinstance(value, bool): kwargs = {"valuetype": "boolean", "booleanvalue": str(value).lower()}
                elif isinstance(value, (int, float)): kwargs = {"valuetype": "float", "value": value}
                elif isinstance(value, str) and value.startswith("="):
                    formula = re.sub(r"\b([A-Z]+\d+):([A-Z]+\d+)\b", r"[.\1:.\2]", value[1:])
                    formula = re.sub(r"(?<![.:])\b([A-Z]+\d+)\b", r"[.\1]", formula)
                    kwargs = {"valuetype": "string", "formula": "of:=" + formula}
                else: kwargs = {"valuetype": "string"}
                cell = TableCell(**kwargs); cell.addElement(P(text=str(value))); row.addElement(cell)
            table.addElement(row)
        target.spreadsheet.addElement(table)
    stream = io.BytesIO(); target.save(stream)
    return stream.getvalue(), "application/vnd.oasis.opendocument.spreadsheet"


def export_delimited(book, delimiter):
    book = checked(book); sheet = book["sheets"][book["active"]]
    used = [(int(re.search(r"\d+", a).group()), a) for a in sheet["cells"]]
    max_row = max((r for r, _ in used), default=1)
    max_col = 1
    for _, address in used:
        letters = re.match(r"[A-Z]+", address).group(); n = 0
        for ch in letters: n = n * 26 + ord(ch) - 64
        max_col = max(max_col, n)
    output = io.StringIO(newline="")
    writer = csv.writer(output, delimiter=delimiter, lineterminator="\n")
    for row in range(1, max_row + 1):
        writer.writerow([cell_value(sheet["cells"].get(f"{col_name(col)}{row}")) for col in range(1, max_col + 1)])
    mime = "text/tab-separated-values; charset=utf-8" if delimiter == "\t" else "text/csv; charset=utf-8"
    return ("\ufeff" + output.getvalue()).encode("utf-8"), mime


def import_workbook(filename, data):
    if not data or len(data) > MAX_BYTES:
        raise ValueError("Choose a nonempty file smaller than 25 MB.")
    ext = Path(filename).suffix.lower()
    name = Path(filename).stem[:80] or "Imported"
    if ext in (".xlsx", ".xlsm", ".xltx"):
        return import_xlsx(data)
    if ext == ".xls": return import_xls(data)
    if ext == ".ods": return import_ods(data)
    if ext == ".csv":
        return delimited_import(data, ",", name)
    if ext == ".tsv":
        return delimited_import(data, "\t", name)
    if ext in (".json", ".plotsheet"):
        try: value = json.loads(data.decode("utf-8-sig"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc: raise ValueError("Invalid PlotlySheets JSON file.") from exc
        return checked(value.get("settings", value)), []
    raise ValueError("Supported imports: XLSX, XLSM, XLTX, XLS, ODS, CSV, TSV and PlotlySheets JSON.")


def export_workbook(book, title, extension):
    extension = extension.lower().lstrip(".")
    if extension == "xlsx": return export_xlsx(book, title)
    if extension == "xls": return export_xls(book, title)
    if extension == "ods": return export_ods(book, title)
    if extension == "csv": return export_delimited(book, ",")
    if extension == "tsv": return export_delimited(book, "\t")
    if extension in ("json", "plotsheet"):
        return json.dumps({"title": title, "settings": checked(book)}, indent=2).encode(), "application/json"
    raise ValueError("Unsupported export format.")
