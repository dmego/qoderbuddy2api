// Package vault implements the credential vault: Fernet encryption plus the
// derived fingerprint key.
//
// The on-disk format must stay byte-compatible with Python's `cryptography`
// Fernet implementation, otherwise the Go rewrite could not read the existing
// credential rows. Fernet spec:
//
//	version(1) || timestamp(8, big endian) || IV(16) || ciphertext || HMAC-SHA256(32)
//
// where ciphertext = AES-128-CBC(PKCS7 pad, key=first 16 raw key bytes) and the
// HMAC covers everything before it using the last 16 raw key bytes.
package vault

import (
	"crypto/aes"
	"crypto/cipher"
	"crypto/hmac"
	"crypto/sha256"
	"encoding/base64"
	"encoding/binary"
	"encoding/hex"
	"encoding/json"
	"errors"
	"fmt"
	"time"
)

const (
	fernetVersion = 0x80
	ivLen         = 16
	hmacLen       = sha256.Size
)

var (
	ErrInvalidToken = errors.New("invalid credential token")
	ErrInvalidKey   = errors.New("invalid Fernet key")
)

// Vault encrypts and decrypts credential payloads.
type Vault struct {
	signingKey []byte // 16 bytes, first half of the decoded key
	cipherKey  []byte // 16 bytes, second half
	fpKey      []byte // sha256(raw key), used for stable non-secret fingerprints
}

// New builds a Vault from a urlsafe-base64 encoded 32-byte Fernet key.
func New(key string) (*Vault, error) {
	raw, err := base64.URLEncoding.DecodeString(key)
	if err != nil {
		return nil, fmt.Errorf("%w: %v", ErrInvalidKey, err)
	}
	if len(raw) != 32 {
		return nil, fmt.Errorf("%w: expected 32 decoded bytes, got %d", ErrInvalidKey, len(raw))
	}
	digest := sha256.Sum256(raw)
	return &Vault{
		signingKey: raw[:16],
		cipherKey:  raw[16:],
		fpKey:      digest[:],
	}, nil
}

// Encrypt serialises payload as compact JSON and returns a Fernet token.
func (v *Vault) Encrypt(payload map[string]any) (string, error) {
	raw, err := json.Marshal(payload)
	if err != nil {
		return "", err
	}
	block, err := aes.NewCipher(v.cipherKey)
	if err != nil {
		return "", err
	}
	padded := pkcs7Pad(raw, aes.BlockSize)
	// version || timestamp || IV || ciphertext || HMAC — reserve the HMAC tail
	// up front, otherwise the body slice below would cut into the ciphertext.
	body := make([]byte, 1+8+ivLen+len(padded))
	body[0] = fernetVersion
	binary.BigEndian.PutUint64(body[1:9], uint64(time.Now().Unix()))
	iv := body[9 : 9+ivLen]
	if _, err := randRead(iv); err != nil {
		return "", err
	}
	cipher.NewCBCEncrypter(block, iv).CryptBlocks(body[9+ivLen:], padded)
	mac := hmac.New(sha256.New, v.signingKey)
	mac.Write(body)
	token := append(body, mac.Sum(nil)...)
	return base64.URLEncoding.EncodeToString(token), nil
}

// Decrypt verifies the HMAC and returns the JSON object payload.
func (v *Vault) Decrypt(blob string) (map[string]any, error) {
	token, err := base64.URLEncoding.DecodeString(blob)
	if err != nil {
		return nil, fmt.Errorf("%w: %v", ErrInvalidToken, err)
	}
	if len(token) < 1+8+ivLen+hmacLen+aes.BlockSize {
		return nil, ErrInvalidToken
	}
	if token[0] != fernetVersion {
		return nil, fmt.Errorf("%w: unknown version %#x", ErrInvalidToken, token[0])
	}
	body, signature := token[:len(token)-hmacLen], token[len(token)-hmacLen:]
	mac := hmac.New(sha256.New, v.signingKey)
	mac.Write(body)
	if !hmac.Equal(mac.Sum(nil), signature) {
		return nil, ErrInvalidToken
	}
	block, err := aes.NewCipher(v.cipherKey)
	if err != nil {
		return nil, err
	}
	iv := body[9 : 9+ivLen]
	ciphertext := body[9+ivLen:]
	if len(ciphertext)%aes.BlockSize != 0 || len(ciphertext) == 0 {
		return nil, ErrInvalidToken
	}
	plain := make([]byte, len(ciphertext))
	cipher.NewCBCDecrypter(block, iv).CryptBlocks(plain, ciphertext)
	plain, err = pkcs7Unpad(plain, aes.BlockSize)
	if err != nil {
		return nil, err
	}
	var payload map[string]any
	if err := json.Unmarshal(plain, &payload); err != nil {
		return nil, fmt.Errorf("%w: payload is not a JSON object", ErrInvalidToken)
	}
	return payload, nil
}

// Fingerprint returns a stable keyed digest of a secret without retaining it.
// Mirrors CredentialVault.fingerprint so previously stored values stay valid.
func (v *Vault) Fingerprint(secret string) string {
	mac := hmac.New(sha256.New, v.fpKey)
	mac.Write([]byte(secret))
	return hex.EncodeToString(mac.Sum(nil))
}

func pkcs7Pad(data []byte, size int) []byte {
	pad := size - len(data)%size
	out := make([]byte, len(data)+pad)
	copy(out, data)
	for i := len(data); i < len(out); i++ {
		out[i] = byte(pad)
	}
	return out
}

func pkcs7Unpad(data []byte, size int) ([]byte, error) {
	if len(data) == 0 || len(data)%size != 0 {
		return nil, ErrInvalidToken
	}
	pad := int(data[len(data)-1])
	if pad == 0 || pad > size || pad > len(data) {
		return nil, ErrInvalidToken
	}
	for _, b := range data[len(data)-pad:] {
		if int(b) != pad {
			return nil, ErrInvalidToken
		}
	}
	return data[:len(data)-pad], nil
}
