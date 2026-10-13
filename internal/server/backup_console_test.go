package server

import (
	"context"
	"encoding/json"
	"net/http"
	"os"
	"testing"

	"github.com/dmego/qoderbuddy2api/internal/store"
)

// TestValidateBackupPreflight proves the console's "校验恢复" button works: the
// preflight re-hashes the snapshot, opens it read-only, runs the integrity
// check and compares the schema generation, then answers 200 with the offline
// next step.
//
// The endpoint was missing entirely, so the console's validate action failed
// with "Request failed with status 404" against a healthy snapshot.
func TestValidateBackupPreflight(t *testing.T) {
	api, db := newTestAPI(t)
	ctx := context.Background()

	// Create a real snapshot through the same handler the console uses.
	created := adminRequest(t, api, http.MethodPost, "/api/admin/backup", nil)
	if created.Code != http.StatusOK {
		t.Fatalf("create backup: %d (%s)", created.Code, created.Body.String())
	}
	var envelope struct {
		Backup store.BackupRun `json:"backup"`
	}
	if err := json.Unmarshal(created.Body.Bytes(), &envelope); err != nil {
		t.Fatalf("decode create response: %v", err)
	}
	if envelope.Backup.SHA256 == nil || *envelope.Backup.SHA256 == "" {
		t.Fatal("backup has no sha256; the validator cannot verify it")
	}

	recorder := adminRequest(t, api, http.MethodPost,
		"/api/admin/backup/"+envelope.Backup.BackupID+"/restore", map[string]any{"dry_run": true})
	if recorder.Code != http.StatusOK {
		t.Fatalf("validate: status %d (%s)", recorder.Code, recorder.Body.String())
	}
	var result struct {
		BackupID      string `json:"backup_id"`
		Valid         bool   `json:"valid"`
		DryRun        bool   `json:"dry_run"`
		SchemaVersion string `json:"schema_version"`
		SizeBytes     int64  `json:"size_bytes"`
		SHA256        string `json:"sha256"`
		NextStep      string `json:"next_step"`
	}
	if err := json.Unmarshal(recorder.Body.Bytes(), &result); err != nil {
		t.Fatalf("decode validate response: %v", err)
	}
	if !result.Valid || !result.DryRun || result.BackupID != envelope.Backup.BackupID {
		t.Fatalf("unexpected validate payload: %+v", result)
	}
	if result.SHA256 != *envelope.Backup.SHA256 {
		t.Errorf("sha256 = %q, want the recorded %q", result.SHA256, *envelope.Backup.SHA256)
	}
	if result.SizeBytes != *envelope.Backup.SizeBytes {
		t.Errorf("size_bytes = %d, want %d", result.SizeBytes, *envelope.Backup.SizeBytes)
	}
	// The snapshot's own schema generation, verified against the live database.
	current, err := db.SchemaVersion(ctx)
	if err != nil {
		t.Fatalf("schema version: %v", err)
	}
	if result.SchemaVersion != current {
		t.Errorf("schema_version = %q, want %q", result.SchemaVersion, current)
	}
	if result.NextStep != "offline_restore_required" {
		t.Errorf("next_step = %q, want offline_restore_required", result.NextStep)
	}
}

// TestValidateBackupRejectsTamperedAndUnknown proves the preflight refuses what
// it cannot vouch for: an unknown id, a snapshot whose bytes no longer match the
// recorded digest, and a real (non-dry) restore request.
func TestValidateBackupRejectsTamperedAndUnknown(t *testing.T) {
	api, db := newTestAPI(t)

	missing := adminRequest(t, api, http.MethodPost,
		"/api/admin/backup/bk-does-not-exist/restore", map[string]any{"dry_run": true})
	if missing.Code != http.StatusNotFound {
		t.Fatalf("unknown backup: status %d, want 404 (%s)", missing.Code, missing.Body.String())
	}

	created := adminRequest(t, api, http.MethodPost, "/api/admin/backup", nil)
	var envelope struct {
		Backup store.BackupRun `json:"backup"`
	}
	if err := json.Unmarshal(created.Body.Bytes(), &envelope); err != nil {
		t.Fatalf("decode create response: %v", err)
	}

	// A real restore is refused: the console copy promises the online control
	// plane never overwrites its own database.
	real := adminRequest(t, api, http.MethodPost,
		"/api/admin/backup/"+envelope.Backup.BackupID+"/restore", map[string]any{"dry_run": false})
	if real.Code != http.StatusConflict {
		t.Fatalf("non-dry restore: status %d, want 409 (%s)", real.Code, real.Body.String())
	}

	// Tampering with the file must fail the digest check.
	handle, err := os.OpenFile(envelope.Backup.Path, os.O_APPEND|os.O_WRONLY, 0o600)
	if err != nil {
		t.Fatalf("open snapshot for tamper: %v", err)
	}
	if _, err := handle.WriteString("tampered"); err != nil {
		_ = handle.Close()
		t.Fatalf("tamper snapshot: %v", err)
	}
	_ = handle.Close()

	tampered := adminRequest(t, api, http.MethodPost,
		"/api/admin/backup/"+envelope.Backup.BackupID+"/restore", map[string]any{"dry_run": true})
	if tampered.Code != http.StatusUnprocessableEntity {
		t.Fatalf("tampered backup: status %d, want 422 (%s)", tampered.Code, tampered.Body.String())
	}

	// A row pointing outside the managed directory must never be read.
	if err := db.RecordBackupRun(context.Background(), store.BackupRun{
		BackupID: "bk-escape", Path: "/etc/hosts", SchemaVersion: "8",
		StartedAt: store.NowISO(), Status: "succeeded",
	}); err != nil {
		t.Fatalf("seed escape row: %v", err)
	}
	escape := adminRequest(t, api, http.MethodPost,
		"/api/admin/backup/bk-escape/restore", map[string]any{"dry_run": true})
	if escape.Code != http.StatusUnprocessableEntity {
		t.Fatalf("out-of-tree path: status %d, want 422 (%s)", escape.Code, escape.Body.String())
	}
}
