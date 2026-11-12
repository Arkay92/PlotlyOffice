"""Local administrator migration: python -m plotlyword.manage recover-legacy.

Old shared-library documents are preserved but hidden until explicitly assigned
to an owner. This command writes an owner key next to the selected database.
"""
import argparse
import os
from pathlib import Path
from .storage import Store


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('command',choices=['recover-legacy'])
    parser.add_argument('--database',type=Path,default=Path(os.environ.get('PLOTLYWORD_DATABASE',Path(__file__).resolve().parents[1]/'data'/'documents.sqlite3')))
    args=parser.parse_args()
    store=Store(args.database)
    output=args.database.parent/'legacy-workspace-key.txt'
    with store.connect() as db:
        db.execute('BEGIN IMMEDIATE')
        count=db.execute("SELECT count(*) FROM documents WHERE owner_id='legacy'").fetchone()[0]
        if not count:
            print('No unowned legacy documents need migration.');return
        import secrets
        key=secrets.token_urlsafe(32)
        # Exclusive creation avoids overwriting an existing recovery key.
        with output.open('x',encoding='utf-8') as f:
            f.write(key+'\n')
        owner=store.key_hash(key)
        db.execute('INSERT INTO identities VALUES (?,?)',(owner,'Workspace owner'))
        db.execute("UPDATE documents SET owner_id=? WHERE owner_id='legacy'",(owner,))
    print(f'Assigned {count} legacy documents to a private workspace.')
    print(f'Recovery key file: {output.resolve()}')
    print('In the editor: Your workspace -> Restore an existing workspace. Keep this key private.')


if __name__=='__main__':main()
