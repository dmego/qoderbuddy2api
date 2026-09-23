// Package importer persists accounts produced by the browser-login import flows.
//
// It is the storage half of oauthflow's ImportWriter contract: the flow layer
// decides what was signed in, this decides how it is stored. It lives in its own
// package so the flow layer's tests can drive the real writer instead of a fake,
// which is the only way to prove a token reaches the database encrypted.
package importer

import (
	"context"
	"crypto/rand"
	"crypto/sha256"
	"encoding/hex"
	"errors"
	"log/slog"
	"strconv"
	"time"

	"github.com/dmego/qoderbuddy2api/internal/models"
	"github.com/dmego/qoderbuddy2api/internal/store"
	"github.com/dmego/qoderbuddy2api/internal/vault"
)

// Writer stores imported accounts and their credentials.
type Writer struct {
	db    *store.DB
	vault *vault.Vault
}

// New builds a writer.
func New(db *store.DB, credVault *vault.Vault) *Writer {
	return &Writer{db: db, vault: credVault}
}

// UpsertImportedAccount stores or updates one signed-in account plus its
// credential and chat purpose, returning the account id.
func (w *Writer) UpsertImportedAccount(
	ctx context.Context,
	provider, accountID, label string,
	identityHash *string,
	payload map[string]any,
	expiresAt string,
) (string, error) {
	if payload == nil {
		return "", errors.New("import payload is empty")
	}
	// The operator's chosen account wins over identity de-duplication: the
	// re-authorization entry point sends the account id, and a credential that
	// names a different upstream account must not silently redirect the write.
	// This is the precedence the pre-rewrite build used (durable_id = account_id
	// or slug), and it is what keeps "re-authorize this account" honest.
	if accountID != "" {
		if existing, err := w.db.GetAccount(ctx, provider, accountID); err == nil && identityHash == nil {
			identityHash = existing.IdentityHash
		}
	} else {
		accountID = deriveAccountID(provider)
		// Reuse the existing row for this identity so a repeat login updates the
		// credential instead of accumulating a duplicate account with its own
		// quota state and its own entry in every routing decision.
		if identityHash != nil {
			if existing, err := w.db.FindAccountByIdentityHash(ctx, provider, *identityHash); err == nil {
				accountID = existing.AccountID
			}
		}
	}
	account, err := w.db.UpsertAccount(ctx, store.Account{
		Provider:       provider,
		AccountID:      accountID,
		Label:          label,
		Source:         "oauth",
		Enabled:        true,
		IdentityHash:   identityHash,
		MaskedIdentity: maskIdentity(label),
	})
	if err != nil {
		return "", err
	}
	encrypted, err := w.vault.Encrypt(payload)
	if err != nil {
		return "", err
	}
	mode := "bearer"
	if _, ok := payload["refresh_token"]; ok {
		mode = "oauth"
	}
	write := store.CredentialWrite{
		Provider:         provider,
		AccountID:        account.AccountID,
		Purpose:          "chat",
		Mode:             mode,
		EncryptedPayload: encrypted,
		HasRefreshToken:  true,
	}
	if expiresAt != "" {
		expires := expiresAt
		write.ExpiresAt = &expires
	}
	if _, err := w.db.UpsertCredential(ctx, write); err != nil {
		return "", err
	}
	now := store.NowISO()
	purpose := store.Purpose{
		Provider:           provider,
		AccountID:          account.AccountID,
		Purpose:            "chat",
		Enabled:            true,
		Status:             "active",
		VerificationStatus: "not_required",
		Capabilities:       []string{"chat"},
		LastSuccessAt:      &now,
	}
	if write.ExpiresAt != nil {
		purpose.ExpiresAt = write.ExpiresAt
	}
	if err := w.db.UpsertPurpose(ctx, purpose); err != nil {
		return "", err
	}
	if err := w.ensureCheckinPurpose(ctx, provider, account.AccountID, write.ExpiresAt); err != nil {
		return "", err
	}
	slog.Info("imported account", "provider", provider, "account", account.AccountID, "source", "oauth")
	return account.AccountID, nil
}

// ensureCheckinPurpose creates the disabled check-in row the domestic import
// flow expects.
//
// The console draws the sign-in control from the presence of this row, and the
// manual check-in import only updates an existing row — it refuses to create
// one. An account imported without it can therefore never start signing in.
// The international deployment has no sign-in centre, so it gets no row at all.
//
// An existing row is left untouched: re-logging in must not reset a credential
// the operator already verified.
func (w *Writer) ensureCheckinPurpose(ctx context.Context, provider, accountID string, expiresAt *string) error {
	if provider != models.ProviderWorkBuddy {
		return nil
	}
	purposes, err := w.db.ListPurposes(ctx, provider, accountID)
	if err != nil {
		return err
	}
	for _, purpose := range purposes {
		if purpose.Purpose == "checkin" {
			return nil
		}
	}
	return w.db.UpsertPurpose(ctx, store.Purpose{
		Provider:           provider,
		AccountID:          accountID,
		Purpose:            "checkin",
		Enabled:            false,
		Status:             "unconfigured",
		VerificationStatus: "unverified",
		Capabilities:       []string{"checkin.workbuddy"},
		ExpiresAt:          expiresAt,
	})
}

// InvalidateCredential drops a cached credential. The Go build resolves
// credentials from storage on each pool reload, so there is no cache to drop.
func (w *Writer) InvalidateCredential(provider, accountID, purpose string) {
	slog.Debug("credential invalidated", "provider", provider, "account", accountID, "purpose", purpose)
}

// deriveAccountID mints the durable id for a newly imported account.
//
// The id is random, matching the pre-rewrite build (uuid4().hex[:12]) and the
// import is de-duplicated through the identity hash instead. A derived id is
// what made two unlabelled logins collide: both computed the same id from the
// same default label, so the second login could only ever update the first
// account.
func deriveAccountID(provider string) string {
	prefix := "acc-"
	switch provider {
	case models.ProviderWorkBuddy:
		prefix = "cb-"
	case models.ProviderWorkBuddyIntl:
		prefix = "wbintl-"
	}
	return prefix + randomHex(12)
}

// randomHex renders n random hex characters (or a timestamp when the system
// entropy source is unavailable, which keeps the id unique in practice).
func randomHex(n int) string {
	raw := make([]byte, (n+1)/2)
	if _, err := rand.Read(raw); err != nil {
		digest := sha256.Sum256([]byte(strconv.FormatInt(time.Now().UnixNano(), 10)))
		return hex.EncodeToString(digest[:])[:n]
	}
	return hex.EncodeToString(raw)[:n]
}

// maskIdentity renders a non-secret display form of an identity.
//
// The mask is derived, never stored raw: an email keeps its first local
// character and its domain, which is enough for an operator to tell accounts
// apart without putting the full address in the console or in any log.
func maskIdentity(value string) *string {
	if value == "" {
		return nil
	}
	masked := "***"
	if at := indexByte(value, '@'); at > 0 {
		local, domain := value[:at], value[at:]
		if len(local) > 1 {
			masked = local[:1] + "***" + domain
		} else {
			masked = "***" + domain
		}
	} else if len(value) > 4 {
		masked = value[:2] + "***" + value[len(value)-2:]
	}
	return &masked
}

func indexByte(value string, target byte) int {
	for index := 0; index < len(value); index++ {
		if value[index] == target {
			return index
		}
	}
	return -1
}
