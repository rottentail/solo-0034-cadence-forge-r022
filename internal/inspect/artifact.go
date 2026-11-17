package inspect

import (
	"cadenceforge/internal/domain"
	"cadenceforge/internal/media"
	"context"
	"crypto/sha256"
	"encoding/hex"
	"fmt"
	"io"
	"math"
	"os"
)

// Contract is the delivery expectation a probed artifact must satisfy. It
// carries only media facts; the inspection capability never sees task state
// such as render attempts, keys or lifecycle.
type Contract struct {
	Format     string
	Width      int
	Height     int
	FPS        int
	SampleRate int
	DurationMS int64
}

// DigestReader hashes every byte the reader yields.
func DigestReader(r io.Reader) (string, int64, error) {
	h := sha256.New()
	n, err := io.Copy(h, r)
	if err != nil {
		return "", n, err
	}
	return hex.EncodeToString(h.Sum(nil)), n, nil
}
func Digest(path string) (string, int64, error) {
	f, err := os.Open(path)
	if err != nil {
		return "", 0, err
	}
	defer f.Close()
	return DigestReader(f)
}

// Check verifies probed media facts against the contract and returns the
// signed duration delta in milliseconds.
func Check(m domain.Media, c Contract) (int64, error) {
	delta := m.DurationMS - c.DurationMS
	tolerance := int64(100)
	if c.Format == "mp4" {
		tolerance = max(tolerance, int64(2000/c.FPS))
	}
	if math.Abs(float64(delta)) > float64(tolerance) {
		return delta, fmt.Errorf("output duration delta %dms exceeds %dms", delta, tolerance)
	}
	if !m.Audio || m.SampleRate != c.SampleRate {
		return delta, fmt.Errorf("output audio contract mismatch")
	}
	if c.Format == "mp4" && (!m.Video || m.Width != c.Width || m.Height != c.Height) {
		return delta, fmt.Errorf("output video contract mismatch")
	}
	return delta, nil
}

// Artifact probes path and verifies it against the contract, returning the
// observed facts. Which task those facts belong to is the caller's concern.
func Artifact(ctx context.Context, prober media.Prober, path string, c Contract) (domain.Inspection, error) {
	m, err := prober.Probe(ctx, path)
	if err != nil {
		return domain.Inspection{}, err
	}
	delta, err := Check(m, c)
	if err != nil {
		return domain.Inspection{}, err
	}
	digest, n, err := Digest(path)
	if err != nil {
		return domain.Inspection{}, err
	}
	return domain.Inspection{Media: m, Digest: digest, Bytes: n, DurationDeltaMS: delta}, nil
}
