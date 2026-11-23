// Package captchasolve implements the client-side transformations behind an
// Aliyun VerifyCaptchaV3 payload: the custom stream cipher, the ali_hash seal,
// the zlib+base64 envelope, and the drag-track transplant that turns a genuine
// SDK capture into a verifiable payload.
//
// It is the Go port of the recipe validated live: three certificates returned
// VerifyCode T001 with this code path (slide/xPos pairs 231/207, 243/228,
// 235/214), and the securityToken produced by the last one was accepted by
// Z.AI on a real chat completion.
//
// The layout mirrors the SDK exactly:
//
//	data  = cipher(base64(zlib(deflate(aliHash(json,"0000") + json))))
//
// where json = {"TrackList":{...},"TrackStartTime":...,"VerifyTime":...,
// "xPos":"...","slidePos":"...","arg":"..."}.
package captchasolve

import (
	"bytes"
	"compress/zlib"
	_ "embed"
	"encoding/base64"
	"encoding/json"
	"errors"
	"fmt"
	"io"
	"math"
	"strconv"
	"strings"
)

// templateData is a genuine VerifyCaptchaV3 data blob captured from the SDK
// (certificate n2Uj9pElgJ). Its track is the pointer/piece template that the
// transplant scales; the certificate itself is long dead.
//
//go:embed template_data.txt
var templateData string

// TemplateData returns the captured payload used as the drag-track template.
func TemplateData() string { return strings.TrimSpace(templateData) }

// permTable is the SDK's 64-byte permutation seed. It must not be mutated.
var permTable = [64]int{
	32, 50, 10, 51, 6, 44, 37, 16, 46, 11, 62, 19, 43, 25, 23, 30,
	60, 33, 53, 34, 7, 26, 12, 48, 5, 2, 20, 4, 61, 13, 47, 49,
	18, 29, 27, 22, 1, 17, 39, 56, 41, 38, 55, 31, 15, 58, 52, 40,
	8, 57, 45, 35, 59, 36, 42, 54, 63, 3, 24, 28, 14, 9, 0, 21,
}

const cipherKey = "3e627e1b4c63f913"

// TrackList is the SDK's drag telemetry. Field order is kept stable so the
// marshalled JSON is deterministic.
type TrackList struct {
	MC        string `json:"mc"`
	TC        string `json:"tc"`
	MU        string `json:"mu"`
	TE        string `json:"te"`
	MP        string `json:"mp"`
	TMV       string `json:"tmv"`
	MM        string `json:"mm"`
	KS        string `json:"ks"`
	FI        string `json:"fi"`
	StartTime int64  `json:"startTime"`
	SI        string `json:"si"`
}

// Inner is the JSON document sealed inside the data blob.
type Inner struct {
	TrackList      TrackList `json:"TrackList"`
	TrackStartTime int64     `json:"TrackStartTime"`
	VerifyTime     int64     `json:"VerifyTime"`
	XPos           string    `json:"xPos"`
	SlidePos       string    `json:"slidePos"`
	Arg            string    `json:"arg"`
}

func ksa(key string) [64]int {
	r := permTable
	t := 0
	for i := 0; i < 64; i++ {
		t = (((i + t + r[i] + r[t]) >> 1) + int(key[i%len(key)])) & 63
		r[i], r[t] = r[t], r[i]
	}
	return r
}

// Encrypt runs the SDK's stream cipher and returns standard base64.
func Encrypt(plain []byte) string {
	r := ksa(cipherKey)
	e, a := 0, 0
	out := make([]byte, len(plain))
	for i, ch := range plain {
		a = ((e ^ a) + (r[e] ^ r[a])) & 63
		r[e], r[a] = r[a], r[e]
		m := (int(ch) + e + r[e] - a - r[a]) & 255
		m ^= r[e] + r[a]
		m ^= r[(r[e]+r[a])&63]
		out[i] = byte(m & 255)
		e = (e + 1) & 63
	}
	return base64.StdEncoding.EncodeToString(out)
}

// Decrypt reverses Encrypt.
func Decrypt(b64 string) ([]byte, error) {
	bin, err := base64.StdEncoding.DecodeString(strings.TrimSpace(b64))
	if err != nil {
		return nil, fmt.Errorf("data is not base64: %w", err)
	}
	r := ksa(cipherKey)
	e, a := 0, 0
	out := make([]byte, len(bin))
	for i, ch := range bin {
		a = ((e ^ a) + (r[e] ^ r[a])) & 63
		r[e], r[a] = r[a], r[e]
		s := e + r[e] - a - r[a]
		x := r[e] + r[a]
		out[i] = byte((int(ch) ^ x ^ r[x&63]) - s)
		e = (e + 1) & 63
	}
	return out, nil
}

// AliHash is the 32-hex-character seal prefixed to the JSON document.
func AliHash(input, salt string) string {
	e := [16]int{}
	for i := 0; i < 16; i++ {
		e[i] = (i << 4) + (i % 16)
	}
	f, m := 16, len(salt)
	j := 0
	for i := 0; i < f; i++ {
		j = (((i + j + e[i] + e[j]) >> 1) + int(salt[i%m])) & (f - 1)
		e[i], e[j] = e[j], e[i]
	}
	p, q := 0, 0
	for idx := 0; idx < len(input); idx++ {
		q = ((p ^ q) + (e[p] ^ e[q])) & (f - 1)
		e[p], e[q] = e[q], e[p]
		c := (int(input[idx]) + p + q) ^ e[p] ^ e[q]
		e[p] = c & 255
		p = (p + 1) & (f - 1)
	}
	for step := 0; step < 2*f; step++ {
		pos := step % f
		if pos != 0 {
			e[pos] ^= e[pos-1]
		} else {
			e[0] ^= e[f-1]
		}
	}
	var b strings.Builder
	for _, v := range e {
		fmt.Fprintf(&b, "%02x", v)
	}
	return b.String()
}

// Unpack decrypts a data blob into its Inner document. The blob decrypts to a
// base64 string whose zlib payload is the sealed JSON.
func Unpack(data string) (*Inner, error) {
	inner0, err := Decrypt(data)
	if err != nil {
		return nil, err
	}
	raw, err := base64.StdEncoding.DecodeString(strings.TrimSpace(string(inner0)))
	if err != nil {
		return nil, fmt.Errorf("inner base64: %w", err)
	}
	zr, err := zlib.NewReader(bytes.NewReader(raw))
	if err != nil {
		return nil, fmt.Errorf("inner zlib: %w", err)
	}
	defer zr.Close()
	plain, err := io.ReadAll(zr)
	if err != nil {
		return nil, fmt.Errorf("inner inflate: %w", err)
	}
	s := string(plain)
	if len(s) <= 32 {
		return nil, errors.New("payload shorter than its 32-byte seal")
	}
	var in Inner
	if err := json.Unmarshal([]byte(s[32:]), &in); err != nil {
		return nil, fmt.Errorf("payload JSON: %w", err)
	}
	return &in, nil
}

// Pack seals and encrypts an Inner document.
func Pack(in *Inner) (string, error) {
	js, err := marshalNoEscape(in)
	if err != nil {
		return "", err
	}
	plain := AliHash(js, "0000") + js
	var buf bytes.Buffer
	zw := zlib.NewWriter(&buf)
	if _, err := zw.Write([]byte(plain)); err != nil {
		return "", err
	}
	if err := zw.Close(); err != nil {
		return "", err
	}
	return Encrypt([]byte(base64.StdEncoding.EncodeToString(buf.Bytes()))), nil
}

func marshalNoEscape(v any) (string, error) {
	var buf bytes.Buffer
	enc := json.NewEncoder(&buf)
	enc.SetEscapeHTML(false)
	if err := enc.Encode(v); err != nil {
		return "", err
	}
	return strings.TrimSuffix(buf.String(), "\n"), nil
}

// Transplant takes the SDK capture (newIn) and replaces its drag telemetry
// with the template's, scaled to the requested pointer delta. hx/hy are the
// handle centre in CSS pixels for the page the puzzle was solved on.
//
// xPos, when negative, is derived from the drag polynomial the SDK uses to
// move the piece: p(h) = 0.00356h² + 0.0752h (floored).
func Transplant(newIn *Inner, slide float64, xPos int, hx, hy float64, tmpl *Inner) (*Inner, error) {
	if newIn == nil || tmpl == nil {
		return nil, errors.New("nil payload")
	}
	mc, err := parseXY(tmpl.TrackList.MC)
	if err != nil {
		return nil, fmt.Errorf("template mc: %w", err)
	}
	mu, err := parseXY(tmpl.TrackList.MU)
	if err != nil {
		return nil, fmt.Errorf("template mu: %w", err)
	}
	base := mu[0] - mc[0]
	if base <= 0 {
		return nil, errors.New("template has a zero drag width")
	}
	scale := slide / base
	toff := (newIn.VerifyTime - newIn.TrackStartTime) - (tmpl.VerifyTime - tmpl.TrackStartTime)

	mapPts := func(s string, timeIdx int) (string, error) {
		if s == "" {
			return "", nil
		}
		pts := strings.Split(s, "|")
		for i, pt := range pts {
			f := strings.Split(pt, ",")
			if len(f) < 2 {
				continue
			}
			x, err1 := strconv.ParseFloat(f[0], 64)
			y, err2 := strconv.ParseFloat(f[1], 64)
			if err1 != nil || err2 != nil {
				continue
			}
			f[0] = strconv.FormatFloat(math.Round(hx+(x-mc[0])*scale), 'f', 0, 64)
			f[1] = strconv.FormatFloat(math.Round(hy+(y-mc[1])), 'f', 0, 64)
			if timeIdx >= 0 && len(f) > timeIdx && f[timeIdx] != "" {
				if t, err := strconv.ParseFloat(f[timeIdx], 64); err == nil {
					f[timeIdx] = strconv.FormatInt(int64(t)+toff, 10)
				}
			}
			pts[i] = strings.Join(f, ",")
		}
		return strings.Join(pts, "|"), nil
	}

	shiftTimes := func(s string) string {
		if s == "" {
			return ""
		}
		pts := strings.Split(s, "|")
		for i, pt := range pts {
			f := strings.Split(pt, ",")
			if len(f) == 0 || f[0] == "" {
				continue
			}
			if t, err := strconv.ParseFloat(f[0], 64); err == nil {
				f[0] = strconv.FormatInt(int64(t)+toff, 10)
			}
			pts[i] = strings.Join(f, ",")
		}
		return strings.Join(pts, "|")
	}

	tl := TrackList{
		TC:        newIn.TrackList.TC,
		TE:        newIn.TrackList.TE,
		TMV:       newIn.TrackList.TMV,
		KS:        newIn.TrackList.KS,
		SI:        newIn.TrackList.SI,
		StartTime: newIn.TrackStartTime,
	}
	if tl.MC, err = mapPts(tmpl.TrackList.MC, 2); err != nil {
		return nil, err
	}
	if tl.MU, err = mapPts(tmpl.TrackList.MU, 2); err != nil {
		return nil, err
	}
	if tl.MP, err = mapPts(tmpl.TrackList.MP, 2); err != nil {
		return nil, err
	}
	if tl.MM, err = mapPts(tmpl.TrackList.MM, 2); err != nil {
		return nil, err
	}
	tl.FI = shiftTimes(tmpl.TrackList.FI)

	if xPos < 0 {
		xPos = int(math.Floor(0.00356*slide*slide + 0.0752*slide + 1e-9))
	}
	slidePos := strconv.Itoa(int(slide))
	if slide != math.Trunc(slide) {
		slidePos = strconv.FormatFloat(slide, 'f', -1, 64)
	}

	return &Inner{
		TrackList:      tl,
		TrackStartTime: newIn.TrackStartTime,
		VerifyTime:     newIn.VerifyTime,
		XPos:           strconv.Itoa(xPos),
		SlidePos:       slidePos,
		Arg:            newIn.Arg,
	}, nil
}

func parseXY(s string) ([2]float64, error) {
	var out [2]float64
	f := strings.Split(s, ",")
	if len(f) < 2 {
		return out, errors.New("point has fewer than two fields")
	}
	x, err := strconv.ParseFloat(f[0], 64)
	if err != nil {
		return out, err
	}
	y, err := strconv.ParseFloat(f[1], 64)
	if err != nil {
		return out, err
	}
	out[0], out[1] = x, y
	return out, nil
}

// DragXPos is the SDK's piece displacement for a pointer delta of h, floored
// to the integer the client reports as xPos.
func DragXPos(h float64) int {
	return int(math.Floor(0.00356*h*h + 0.0752*h + 1e-9))
}

// PointerDelta inverts DragXPos: the integer pointer delta whose piece
// displacement is closest to target CSS pixels.
func PointerDelta(target float64) int {
	if target <= 0 {
		return 0
	}
	const a, b = 0.00356, 0.0752
	disc := b*b + 4*a*target
	h := (-b + math.Sqrt(disc)) / (2 * a)
	return int(math.Round(h))
}
