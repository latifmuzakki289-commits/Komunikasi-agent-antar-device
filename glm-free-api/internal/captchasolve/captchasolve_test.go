package captchasolve

import (
	"math"
	"strconv"
	"strings"
	"testing"
)

func mustTemplate(t *testing.T) *Inner {
	t.Helper()
	tmpl, err := Unpack(TemplateData())
	if err != nil {
		t.Fatalf("unpack template: %v", err)
	}
	if tmpl.VerifyTime <= tmpl.TrackStartTime {
		t.Fatalf("template has non-positive verify window: %+v", tmpl)
	}
	return tmpl
}

func TestTemplateRoundTrip(t *testing.T) {
	tmpl := mustTemplate(t)
	data, err := Pack(tmpl)
	if err != nil {
		t.Fatalf("pack: %v", err)
	}
	back, err := Unpack(data)
	if err != nil {
		t.Fatalf("unpack packed: %v", err)
	}
	if back.Arg != tmpl.Arg || back.XPos != tmpl.XPos || back.SlidePos != tmpl.SlidePos {
		t.Fatalf("scalar fields drifted: %+v vs %+v", back, tmpl)
	}
	if back.VerifyTime != tmpl.VerifyTime || back.TrackStartTime != tmpl.TrackStartTime {
		t.Fatalf("timestamps drifted")
	}
	if strings.Count(back.TrackList.MM, "|") != strings.Count(tmpl.TrackList.MM, "|") {
		t.Fatalf("mm point count drifted")
	}
	if back.TrackList.MC != tmpl.TrackList.MC || back.TrackList.MU != tmpl.TrackList.MU {
		t.Fatalf("track endpoints drifted: %q/%q", back.TrackList.MC, back.TrackList.MU)
	}
}

func TestTransplantScalesToRequestedSlide(t *testing.T) {
	tmpl := mustTemplate(t)

	newIn := *tmpl // value copy; TrackList is all strings/scalars
	newIn.TrackStartTime = tmpl.TrackStartTime + 12345
	newIn.VerifyTime = tmpl.VerifyTime + 12345
	newIn.Arg = "AAAAAAAAAAAAAA=="

	const (
		hx, hy = 310.0, 420.0
		slide  = 235.0
	)
	out, err := Transplant(&newIn, slide, -1, hx, hy, tmpl)
	if err != nil {
		t.Fatalf("transplant: %v", err)
	}
	if out.Arg != newIn.Arg {
		t.Fatalf("arg not preserved: %q", out.Arg)
	}
	if out.XPos != strconv.Itoa(DragXPos(slide)) {
		t.Fatalf("xPos = %s, want %d", out.XPos, DragXPos(slide))
	}
	if out.VerifyTime-out.TrackStartTime != newIn.VerifyTime-newIn.TrackStartTime {
		t.Fatalf("verify window changed")
	}

	mc, err := parseXY(out.TrackList.MC)
	if err != nil {
		t.Fatalf("parse mc: %v", err)
	}
	mu, err := parseXY(out.TrackList.MU)
	if err != nil {
		t.Fatalf("parse mu: %v", err)
	}
	if math.Abs(mc[0]-hx) > 1 || math.Abs(mc[1]-hy) > 1 {
		t.Fatalf("start point = %v, want near (%v,%v)", mc, hx, hy)
	}
	if got := mu[0] - mc[0]; math.Abs(got-slide) > 1 {
		t.Fatalf("scaled width = %v, want %v", got, slide)
	}

	data, err := Pack(out)
	if err != nil {
		t.Fatalf("pack transplanted: %v", err)
	}
	back, err := Unpack(data)
	if err != nil {
		t.Fatalf("unpack transplanted: %v", err)
	}
	if back.XPos != out.XPos || back.SlidePos != out.SlidePos {
		t.Fatalf("transplanted payload did not round-trip")
	}
}

func TestDragPolynomial(t *testing.T) {
	cases := []struct {
		target float64
		want   int
	}{
		{214.9, 235}, // validated live: T001
		{228.1, 243}, // validated live: T001
		{206.8, 231}, // validated live: T001
	}
	for _, c := range cases {
		if got := PointerDelta(c.target); got != c.want {
			t.Fatalf("PointerDelta(%v) = %d, want %d", c.target, got, c.want)
		}
	}
	if got := DragXPos(235); got != 214 {
		t.Fatalf("DragXPos(235) = %d, want 214", got)
	}
}
