import io
import json
import re
from pathlib import Path
import pandas as pd

MAX_BYTES = 20 * 1024 * 1024
MAX_ROWS = 100_000

def clean(df):
    if df.empty or not len(df.columns):
        raise ValueError('This source contains no rows.')
    if len(df) > MAX_ROWS or len(df.columns) > 150:
        raise ValueError('Each table supports up to 100,000 rows and 150 columns. Split this source first.')
    df = df.copy().replace([float('inf'),float('-inf')],float('nan'))
    names, seen = [], set()
    for col in df.columns:
        base = str(col).strip() or 'column'
        name, n = base, 2
        while name in seen:
            name = f'{base}_{n}'; n += 1
        seen.add(name); names.append(name)
    df.columns = names
    for col in df:
        if df[col].dtype == object:
            df[col] = df[col].map(lambda v: json.dumps(v) if isinstance(v, (dict, list)) else v)
            if re.search(r'date|timestamp|created_at|time$', col, re.I):
                parsed = pd.to_datetime(df[col], errors='coerce', utc=True)
                if parsed.notna().sum() >= df[col].notna().sum() * .9:
                    df[col] = parsed.dt.strftime('%Y-%m-%dT%H:%M:%SZ')
    return df

def json_table(value, path=''):
    for part in path.split('.') if path else []:
        value = value[int(part)] if isinstance(value, list) else value[part]
    if isinstance(value, dict):
        lists = [v for v in value.values() if isinstance(v, list)]
        if len(lists) == 1:
            value = lists[0]
        else:
            value = [value]
    if not isinstance(value, list):
        raise ValueError('Select a JSON path containing records, for example data.items.')
    return clean(pd.json_normalize(value))

def parse_file(name, raw):
    if len(raw) > MAX_BYTES:
        raise ValueError('Each file must be 20 MB or smaller.')
    ext = Path(name).suffix.lower()
    buf = io.BytesIO(raw)
    if ext in {'.xlsx', '.xls', '.ods'}:
        sheets = pd.read_excel(buf, sheet_name=None, nrows=MAX_ROWS+1)
        return {f'{name} · {sheet}': clean(df) for sheet, df in sheets.items() if not df.empty}
    if ext in {'.parquet', '.pq'}:
        df = pd.read_parquet(buf)
    elif ext == '.feather':
        df = pd.read_feather(buf)
    elif ext in {'.jsonl', '.ndjson'}:
        df = pd.json_normalize([json.loads(line) for line in raw.decode('utf-8-sig').splitlines() if line.strip()])
    elif ext == '.json':
        df = json_table(json.loads(raw.decode('utf-8-sig')))
    elif ext in {'.csv', '.tsv', '.txt'}:
        try:
            text = raw.decode('utf-8-sig')
        except UnicodeDecodeError:
            text = raw.decode('cp1252')
        df = pd.read_csv(io.StringIO(text), sep='\t' if ext == '.tsv' else None, engine='python', nrows=MAX_ROWS + 1)
    else:
        raise ValueError('Unsupported format. Use CSV, TSV, JSON, JSONL, Excel, ODS, Parquet or Feather. Export PDFs/images as a table first.')
    return {name: clean(df)}

def pack(df):
    return json.loads(df.to_json(orient='split', date_format='iso'))

def unpack(value):
    return pd.DataFrame(value['data'], columns=value['columns'])

def profile(name, df, samples=False):
    result = {'name': name, 'rows': len(df), 'columns': [{'name': c, 'type': str(df[c].dtype), 'unique': int(df[c].nunique()), 'missing': int(df[c].isna().sum())} for c in df]}
    if samples:
        result['sample'] = json.loads(df.head(5).to_json(orient='records', date_format='iso'))
    return result

def relationships(tables):
    found = []
    norm = lambda s: re.sub(r'[^a-z0-9]', '', s.lower())
    names = {name:{norm(c):c for c in df} for name,df in tables.items()}
    values = {}
    unique = {(name,c):df[c].notna().all() and df[c].is_unique for name,df in tables.items() for c in df}
    def keys(name, column):
        key = (name,column)
        if key not in values: values[key] = set(tables[name][column].dropna().astype(str))
        return values[key]
    for left, a in tables.items():
        for right, b in tables.items():
            if left == right:
                continue
            for common in names[left].keys() & names[right].keys():
                x,y = names[left][common],names[right][common]
                if not unique[right,y]: continue
                av,bv = keys(left,x),keys(right,y)
                overlap = len(av & bv) / max(1,len(av))
                if overlap >= .5:
                    found.append({'left': left, 'right': right, 'left_on': x, 'right_on': y, 'coverage': round(overlap, 3), 'cardinality': 'many-to-one'})
                    if len(found) == 40: return found
    return found

def blend(tables, joins):
    result = dict(tables)
    lineage = []
    if not isinstance(joins, list) or len(joins) > 8:
        raise ValueError('A dashboard supports up to eight blends.')
    for j in joins:
        left, right, key, other, alias = (j[k] for k in ('left', 'right', 'left_on', 'right_on', 'name'))
        if alias in result or not isinstance(alias, str) or len(alias) > 120:
            raise ValueError('Blend names must be unique.')
        a, b = result[left].copy(), result[right].copy()
        if b[other].isna().any() or not b[other].is_unique:
            raise ValueError(f'Cannot blend {right}: {other} must be unique and non-null to avoid multiplying totals.')
        a[key], b[other] = a[key].astype('string'), b[other].astype('string')
        merged = a.merge(b, left_on=key, right_on=other, how='left', suffixes=('', '__' + alias), validate='many_to_one', indicator=True)
        matched = int((merged['_merge'] == 'both').sum())
        if not matched:
            raise ValueError(f'Blend {alias} has no matching keys.')
        result[alias] = merged.drop(columns='_merge')
        lineage.append({**j, 'matched': matched, 'rows': len(a), 'unmatched': len(a)-matched})
    return result, lineage
