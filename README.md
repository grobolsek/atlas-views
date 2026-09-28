# atlas-views

Keeps MySQL views in Atlas migration files, without Atlas Pro. It's a stopgap until dbt supports MySQL or you move to Atlas Pro.

```
views/                 one view per .sql file
  active_users.sql     bare SELECT (view name = file name) or a full CREATE VIEW ...
migrations/            Atlas migrations + atlas.sum
atlas.hcl
```

## Install

```bash
pip install ./atlas-views        # or: pip install -e ./atlas-views
```

## Workflow

```bash
atlas migrate diff --env sqlalchemy add_orders   # Atlas writes the table changes
atlas-views sync                                 # views go into that same file, then re-hash
atlas-views check                                # throwaway MySQL: apply everything, test every view
atlas migrate apply --env prod                   # as usual
```

To revert:

```bash
atlas-views down --dev-url docker://mysql/8/dev                  # 1 step, DB from $MYSQL_DATABASE_URL
atlas-views down --to-version 20260924074911 --env prod
```

## Commands

| Command       | What it does                                                                                                                                                                                                                                                                                                    |
| ------------- | --------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| `sync`      | Writes view changes into the migrations and runs`atlas migrate hash`. Flags: `--dry-run`, `--new` (always a new file), `--amend` (rewrite the block in the latest migration), `--no-refresh`, `--name`                                                                                              |
| `check`     | Starts MySQL in Docker, runs`atlas migrate apply`, then runs `SELECT * FROM <view> LIMIT 0` on every view. Also fails if `views/` has unsynced changes. `--to-version V`, `--image`, `--scratch-url` (use an empty DB instead of Docker), `--url` (read-only check of an existing DB), `--keep` |
| `down [N]`  | Runs`atlas migrate down`, then `reconcile`. Anything after `--` is passed to atlas                                                                                                                                                                                                                        |
| `reconcile` | Sets the DB's views to the state at its current migration version (or`--version`)                                                                                                                                                                                                                             |
| `state`     | Lists views at a version (`--version`), which file defined each one, and their history. `--sql` prints definitions                                                                                                                                                                                          |

Exit codes: `0` ok, `1` check found problems, `2` error.

## How it works

**Migration files are the log.** View SQL lives in a marked block at the end of a migration:

```sql
-- atlas-views:begin
-- view: active_users (changed)
CREATE OR REPLACE VIEW `active_users` AS SELECT ...;
-- atlas-views:end
```

The view state at any version comes from replaying the blocks up to that version. There's no separate state file that can drift.

**Which file `sync` writes to.** Migrations after the last file that has a block are "new", meaning Atlas generated them since the last sync. Sync appends to the latest of them. If there are none, it creates a file with `atlas migrate new views`. Every new file gets a block, even if it only says `-- no view changes`, so it's marked as processed. The first sync always creates a new file, so already-applied migrations are never touched.

**What changes produce SQL.** New and changed views become `CREATE OR REPLACE VIEW`. Files removed from `views/` become `DROP VIEW IF EXISTS`. Edits that only change formatting or comments produce nothing.

**Dependencies.** `sync` reads the table DDL in the new migrations (`ALTER`, `DROP`, `RENAME`, `CREATE TABLE`). It re-creates, with a warning, every view that references those tables, and every view that references a changed, dropped or refreshed view (transitively). A view broken by a column rename then fails at `migrate apply` and in `check`, not silently at query time. The refresh also picks up new columns for `SELECT *` views. Use `--no-refresh` to only warn. Views are always emitted after the views they reference.

**Downgrade.** Without Pro, `atlas migrate down` ignores views, so `down` reconciles afterwards. It runs `CREATE OR REPLACE` on each view as of the reverted version and `DROP`s managed views that didn't exist yet. Views that atlas-views never created are left alone, with a warning.

## Configuration

Precedence: flag > environment variable > `[tool.atlas-views]` in `./pyproject.toml` > default.

| Setting                  | Flag                 | Env                            | Default            |
| ------------------------ | -------------------- | ------------------------------ | ------------------ |
| views folder             | `--views-dir`      | `ATLAS_VIEWS_DIR`            | `views`          |
| migrations folder        | `--migrations-dir` | `ATLAS_VIEWS_MIGRATIONS_DIR` | `migrations`     |
| atlas binary             | `--atlas`          | `ATLAS_BIN`                  | `atlas`          |
| dev DB for`down`       | `--dev-url`        | `ATLAS_DEV_URL`              | –                 |
| MySQL image for`check` | `--image`          | `ATLAS_VIEWS_IMAGE`          | `mysql:8.0`      |
| database URL             | `--url`            | `MYSQL_DATABASE_URL`         | –                 |
| TLS CA bundle            | `--ssl-ca`         | `MYSQL_SSL_CA`               | system trust store |

```toml
[tool.atlas-views]
views-dir = "db/views"
migrations-dir = "db/migrations"
dev-url = "docker://mysql/8/dev"
image = "mysql:8.0.40"      # match SELECT VERSION() on the server
```

Azure URL (the same string works for atlas and atlas-views; URL-encode special characters in the password):

```
mysql://USER:PASSWORD@mysql-ki-ds-s.mysql.database.azure.com:3306/DBNAME?tls=true
```

## Rules and limits

- `sync` edits only migrations that haven't been applied yet. Run it right after `atlas migrate diff`, before applying anywhere. Atlas rejects changed files that are already applied.
- `sync --amend` has the same rule: use it to fix a view after `check` fails, while the migration is still unapplied.
- Don't hand-edit the blocks. Change `views/` and re-sync.
- One statement per view file. View names are case-insensitive.
- Dependency detection matches identifiers by name. It can over-refresh, which is harmless, but it won't miss a referenced table.
- `check` needs Docker, or use `--scratch-url` with an empty database. To mirror Azure's case handling, add `--mysqld-arg=--lower-case-table-names=1`.
- If `atlas migrate down` isn't available in your Atlas build, revert the tables another way and run `atlas-views reconcile`.

## Tests

```bash
pip install -e ".[test]"
pytest                                                          # unit tests
ATLAS_VIEWS_TEST_URL='mysql://root:pw@127.0.0.1:3306/mysql' pytest   # + end-to-end against a real server
```

The end-to-end tests use `tests/fake_atlas.py` in place of atlas (`migrate new/hash/apply/down`, with checksum validation).
