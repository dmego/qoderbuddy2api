// Package store: database handle, migrations and transaction helpers.
package store

import (
	"context"
	"database/sql"
	"fmt"
	"os"
	"path/filepath"
	"strings"
	"sync"
	"time"

	_ "modernc.org/sqlite"
)

// ISOFormat is the exact layout Python's
// `datetime.now(UTC).replace(microsecond=0).isoformat()` produces.
//
// This is NOT RFC3339: Python emits the "+00:00" offset, never "Z". Every
// timestamp column in this database is compared lexicographically against
// values written by the Control Plane, so a "Z" suffix would silently break
// range filters (">=" on '2026-09-16T00:00:00Z' misses '+00:00' rows in one
// direction and the 'T' vs ' ' split broke SQLite's own datetime() helpers).
const ISOFormat = "2006-01-02T15:04:05+00:00"

// NowISO returns the current UTC instant in the stored timestamp format.
func NowISO() string { return time.Now().UTC().Format(ISOFormat) }

// FormatISO renders a moment in the stored timestamp format.
func FormatISO(t time.Time) string { return t.UTC().Format(ISOFormat) }

// ParseISO accepts the stored format, RFC3339 and trailing-Z variants.
func ParseISO(value string) (time.Time, bool) {
	value = strings.TrimSpace(value)
	if value == "" {
		return time.Time{}, false
	}
	for _, layout := range []string{ISOFormat, time.RFC3339Nano, time.RFC3339, "2006-01-02T15:04:05", "2006-01-02 15:04:05"} {
		if parsed, err := time.Parse(layout, value); err == nil {
			return parsed.UTC(), true
		}
	}
	return time.Time{}, false
}

// DB wraps one SQLite connection pool plus the serialization primitives the
// Python repository used (single writer, generous busy timeout).
type DB struct {
	*sql.DB
	path string

	// writeMu serializes write transactions the way the Python repository's
	// single operation lock did; SQLite has one writer anyway and this removes
	// SQLITE_BUSY retries entirely.
	writeMu sync.Mutex
}

// Open connects to the database, applies the pragmas the Python Control Plane
// used, and runs migrate().
func Open(path string) (*DB, error) {
	if dir := filepath.Dir(path); dir != "" && dir != "." {
		if err := os.MkdirAll(dir, 0o700); err != nil {
			return nil, fmt.Errorf("create data dir: %w", err)
		}
	}
	dsn := path + "?_pragma=busy_timeout(5000)&_pragma=journal_mode(WAL)&_pragma=synchronous(NORMAL)&_pragma=foreign_keys(ON)"
	handle, err := sql.Open("sqlite", dsn)
	if err != nil {
		return nil, fmt.Errorf("open sqlite: %w", err)
	}
	// The Python repository serialized every operation through one lock; a
	// single connection reproduces that and avoids WAL write contention.
	handle.SetMaxOpenConns(1)
	handle.SetMaxIdleConns(1)
	handle.SetConnMaxLifetime(0)

	db := &DB{DB: handle, path: path}
	ctx, cancel := context.WithTimeout(context.Background(), 30*time.Second)
	defer cancel()
	if err := db.PingContext(ctx); err != nil {
		_ = handle.Close()
		return nil, fmt.Errorf("ping sqlite: %w", err)
	}
	if err := db.Migrate(ctx); err != nil {
		_ = handle.Close()
		return nil, err
	}
	return db, nil
}

// Path returns the database file path.
func (d *DB) Path() string { return d.path }

// Migrate creates the schema and applies additive column migrations. It is
// idempotent and safe against a database written by the Python control plane.
func (d *DB) Migrate(ctx context.Context) error {
	d.writeMu.Lock()
	defer d.writeMu.Unlock()
	if _, err := d.ExecContext(ctx, baseSchema); err != nil {
		return fmt.Errorf("apply schema: %w", err)
	}
	for _, migration := range migrations {
		if err := d.ensureColumn(ctx, migration.Table, migration.Column, migration.Definition); err != nil {
			return err
		}
	}
	if _, err := d.ExecContext(ctx,
		"INSERT INTO schema_meta(key, value) VALUES('schema_version', ?) "+
			"ON CONFLICT(key) DO UPDATE SET value=excluded.value", schemaVersion); err != nil {
		return fmt.Errorf("write schema version: %w", err)
	}
	if _, err := d.ExecContext(ctx,
		"UPDATE credentials SET has_refresh_token=1 WHERE provider IN ("+placeholders(len(refreshableProviders))+") AND has_refresh_token=0",
		stringsToAny(refreshableProviders)...); err != nil {
		return fmt.Errorf("backfill refreshable flags: %w", err)
	}
	return nil
}

func (d *DB) ensureColumn(ctx context.Context, table, column, definition string) error {
	rows, err := d.QueryContext(ctx, "PRAGMA table_info("+table+")")
	if err != nil {
		return fmt.Errorf("inspect %s: %w", table, err)
	}
	defer rows.Close()
	columns, err := rows.Columns()
	if err != nil {
		return err
	}
	nameIndex := -1
	for i, name := range columns {
		if name == "name" {
			nameIndex = i
			break
		}
	}
	if nameIndex < 0 {
		return fmt.Errorf("PRAGMA table_info(%s) returned no name column", table)
	}
	exists := false
	for rows.Next() {
		values, err := scanRow(rows, len(columns))
		if err != nil {
			return err
		}
		if name, ok := values[nameIndex].(string); ok && name == column {
			exists = true
		}
	}
	if err := rows.Err(); err != nil {
		return err
	}
	if exists {
		return nil
	}
	if _, err := d.ExecContext(ctx, "ALTER TABLE "+table+" ADD COLUMN "+column+" "+definition); err != nil {
		return fmt.Errorf("add %s.%s: %w", table, column, err)
	}
	return nil
}

// Write runs fn inside a write transaction with the single-writer lock held.
func (d *DB) Write(ctx context.Context, fn func(*sql.Tx) error) error {
	d.writeMu.Lock()
	defer d.writeMu.Unlock()
	tx, err := d.BeginTx(ctx, nil)
	if err != nil {
		return err
	}
	if err := fn(tx); err != nil {
		_ = tx.Rollback()
		return err
	}
	return tx.Commit()
}

// Read runs fn against the shared read connection.
func (d *DB) Read(ctx context.Context, fn func(*sql.Conn) error) error {
	conn, err := d.Conn(ctx)
	if err != nil {
		return err
	}
	defer conn.Close()
	return fn(conn)
}

// Checkpoint truncates the WAL back into the main database file.
func (d *DB) Checkpoint(ctx context.Context) error {
	_, err := d.ExecContext(ctx, "PRAGMA wal_checkpoint(TRUNCATE)")
	return err
}

// Vacuum compacts the database after large deletions.
func (d *DB) Vacuum(ctx context.Context) error {
	d.writeMu.Lock()
	defer d.writeMu.Unlock()
	_, err := d.ExecContext(ctx, "VACUUM")
	return err
}

// BackupTo writes an online backup to destination using SQLite's own backup API.
func (d *DB) BackupTo(ctx context.Context, destination string) error {
	d.writeMu.Lock()
	defer d.writeMu.Unlock()
	if _, err := d.ExecContext(ctx, "VACUUM INTO ?", destination); err != nil {
		return fmt.Errorf("backup: %w", err)
	}
	return nil
}

func scanRow(rows *sql.Rows, count int) ([]any, error) {
	values := make([]any, count)
	holders := make([]any, count)
	for i := range holders {
		holders[i] = &values[i]
	}
	if err := rows.Scan(holders...); err != nil {
		return nil, err
	}
	return values, nil
}

func placeholders(n int) string {
	if n <= 0 {
		return "''"
	}
	return strings.TrimSuffix(strings.Repeat("?,", n), ",")
}

func stringsToAny(values []string) []any {
	out := make([]any, len(values))
	for i, value := range values {
		out[i] = value
	}
	return out
}

// NullString renders a nullable TEXT column as "" when NULL.
func NullString(value sql.NullString) string { return value.String }

// NullInt renders a nullable INTEGER column as nil when NULL.
func NullInt(value sql.NullInt64) *int64 {
	if !value.Valid {
		return nil
	}
	v := value.Int64
	return &v
}
