package service

import (
	"archive/zip"
	"cadenceforge/internal/domain"
	"cadenceforge/internal/inspect"
	"context"
	"encoding/json"
	"fmt"
	"io"
	"os"
	"path/filepath"
	"time"
)

// contractOf maps a render's frozen facts to an inspection contract. The
// service owns task state; the inspection capability only sees the contract.
func contractOf(r domain.Render) inspect.Contract {
	return inspect.Contract{
		Format:     r.Recipe.Format,
		Width:      r.Recipe.Width,
		Height:     r.Recipe.Height,
		FPS:        r.Recipe.FPS,
		SampleRate: r.Recipe.SampleRate,
		DurationMS: r.Plan.DurationMS,
	}
}

// Delivery owns one verified delivery of a committed artifact: the media
// handle and caption bytes it hands out are exactly the bytes that matched
// the committed inspection. Its lifecycle is a single delivery operation and
// it is never shared between requests, so there is nothing to invalidate
// when external recovery replaces files — the next delivery re-verifies
// against the committed inspection.
type Delivery struct {
	Render     domain.Render
	Inspection domain.Inspection
	Media      *os.File
	MediaMod   time.Time
	Captions   []byte
	CaptionMod time.Time
}

func (d *Delivery) Close() error { return d.Media.Close() }

// Deliver resolves the committed attempt of a succeeded render and verifies
// the on-disk bytes against the committed inspection before handing them
// out. Exactly one complete probe runs per delivery.
func (s *Service) Deliver(ctx context.Context, id string) (*Delivery, error) {
	st, err := s.Store.Snapshot()
	if err != nil {
		return nil, err
	}
	r, ok := st.Renders[id]
	if !ok {
		return nil, domain.Missing("render", id)
	}
	if r.State != "succeeded" || r.Artifact == nil {
		return nil, domain.Conflict("render has no committed artifact")
	}
	dir, err := s.Blobs.RenderDir(id, len(r.Attempts))
	if err != nil {
		return nil, err
	}
	mediaPath := filepath.Join(dir, r.Artifact.Filename)
	media, err := os.Open(mediaPath)
	if err != nil {
		return nil, err
	}
	keepOpen := false
	defer func() {
		if !keepOpen {
			media.Close()
		}
	}()
	m, err := s.Probe.Probe(ctx, mediaPath)
	if err != nil {
		return nil, err
	}
	delta, err := inspect.Check(m, contractOf(r))
	if err != nil {
		return nil, err
	}
	digest, n, err := inspect.DigestReader(media)
	if err != nil {
		return nil, err
	}
	fresh := domain.Inspection{Media: m, Digest: digest, Bytes: n, DurationDeltaMS: delta}
	if digest != r.Artifact.Inspection.Digest || n != r.Artifact.Inspection.Bytes {
		return nil, fmt.Errorf("committed artifact integrity failed")
	}
	captionPath := filepath.Join(dir, r.Artifact.CaptionFile)
	captions, err := os.ReadFile(captionPath)
	if err != nil {
		return nil, err
	}
	if string(captions) != r.Plan.VTT {
		return nil, fmt.Errorf("caption integrity failed")
	}
	info, err := media.Stat()
	if err != nil {
		return nil, err
	}
	captionInfo, err := os.Stat(captionPath)
	if err != nil {
		return nil, err
	}
	if _, err = media.Seek(0, io.SeekStart); err != nil {
		return nil, err
	}
	keepOpen = true
	return &Delivery{Render: r, Inspection: fresh, Media: media, MediaMod: info.ModTime(), Captions: captions, CaptionMod: captionInfo.ModTime()}, nil
}

// Verify re-inspects the committed artifact on demand and reports the fresh
// facts without delivering bytes.
func (s *Service) Verify(ctx context.Context, id string) (domain.Inspection, error) {
	d, err := s.Deliver(ctx, id)
	if err != nil {
		return domain.Inspection{}, err
	}
	defer d.Close()
	return d.Inspection, nil
}

// Bundle exports the verified media, captions and frozen manifest of the
// same successful attempt as one ZIP.
func (s *Service) Bundle(ctx context.Context, id string, dest io.Writer) error {
	d, err := s.Deliver(ctx, id)
	if err != nil {
		return err
	}
	defer d.Close()
	z := zip.NewWriter(dest)
	fail := func(err error) error {
		z.Close()
		return err
	}
	w, err := z.Create(d.Render.Artifact.Filename)
	if err == nil {
		_, err = io.Copy(w, d.Media)
	}
	if err != nil {
		return fail(err)
	}
	w, err = z.Create(d.Render.Artifact.CaptionFile)
	if err == nil {
		_, err = w.Write(d.Captions)
	}
	if err != nil {
		return fail(err)
	}
	manifest, err := json.MarshalIndent(d.Render, "", "  ")
	if err != nil {
		return fail(err)
	}
	w, err = z.Create("composition.json")
	if err == nil {
		_, err = w.Write(manifest)
	}
	if err != nil {
		return fail(err)
	}
	return z.Close()
}
