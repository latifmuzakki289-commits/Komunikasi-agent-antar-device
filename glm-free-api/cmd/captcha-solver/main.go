// Command captcha-solver serves one Aliyun VerifyCaptchaV3 payload at a time
// from a real browser challenge.
//
// The bridge cannot synthesise a verifiable payload on its own: the SDK's
// `arg` is bound to the certificate and cannot be derived from it (the legacy
// generateArg() is provably wrong), and the drag telemetry must be consistent
// with the reported position. This service therefore
//
//  1. boots the Aliyun captcha SDK in a real (headed) Chromium page,
//  2. measures the puzzle gap from the actual images,
//  3. drags the slider, intercepts the SDK's own VerifyCaptchaV3 request,
//  4. replaces its telemetry with a template track scaled to the drag,
//  5. submits the transplanted payload and returns the securityToken param.
//
// POST /solve  {"sceneId":"..."} -> {"param":"<base64 captcha_verify_param>"}
// GET  /health -> {"ok":true,...}
package main

import (
	"encoding/base64"
	"encoding/json"
	"flag"
	"fmt"
	"log"
	"math"
	"net/http"
	"net/url"
	"os"
	"os/signal"
	"strconv"
	"strings"
	"sync"
	"sync/atomic"
	"syscall"
	"time"

	"github.com/mxschmitt/playwright-go"

	"zai-api/internal/captchasolve"
	"zai-api/internal/zbridge"
)

const sdkURL = "https://o.alicdn.com/captcha-frontend/aliyunCaptcha/AliyunCaptcha.js"

type config struct {
	addr     string
	scene    string
	prefix   string
	region   string
	pageURL  string
	headless bool
	attempts int
}

type solver struct {
	cfg     config
	browser playwright.Browser

	mu      sync.Mutex // one challenge at a time
	solves  int64
	fails   int64
	lastErr string
}

func main() {
	cfg := config{}
	flag.StringVar(&cfg.addr, "addr", "127.0.0.1:20251", "listen address")
	flag.StringVar(&cfg.scene, "scene", "didk33e0", "Aliyun scene id")
	flag.StringVar(&cfg.prefix, "prefix", "no8xfe", "captcha prefix")
	flag.StringVar(&cfg.region, "region", "sgp", "captcha region")
	flag.StringVar(&cfg.pageURL, "page", "https://chat.z.ai/", "page that hosts the SDK (its origin matters)")
	flag.BoolVar(&cfg.headless, "headless", false, "run Chromium headless (Aliyun may reject the session)")
	flag.IntVar(&cfg.attempts, "attempts", 3, "challenges to try per request")
	flag.Parse()
	zbridge.SetVerbose(true) // log VerifyCaptchaV3 verdicts

	if err := playwright.Install(&playwright.RunOptions{Browsers: []string{"chromium"}}); err != nil {
		log.Printf("playwright install: %v (continuing)", err)
	}
	pw, err := playwright.Run()
	if err != nil {
		log.Fatalf("playwright run: %v", err)
	}
	defer pw.Stop()

	// Headed is the configuration Aliyun trusts; keep the automation flag out
	// of the engine exactly like the token collector does.
	browser, err := pw.Chromium.Launch(playwright.BrowserTypeLaunchOptions{
		Headless:          playwright.Bool(cfg.headless),
		Args:              []string{"--disable-blink-features=AutomationControlled"},
		IgnoreDefaultArgs: []string{"--enable-automation"},
	})
	if err != nil {
		log.Fatalf("browser launch: %v", err)
	}
	defer browser.Close()

	s := &solver{cfg: cfg, browser: browser}

	mux := http.NewServeMux()
	mux.HandleFunc("/health", s.handleHealth)
	mux.HandleFunc("/solve", s.handleSolve)

	srv := &http.Server{Addr: cfg.addr, Handler: mux, ReadHeaderTimeout: 10 * time.Second}
	go func() {
		ch := make(chan os.Signal, 1)
		signal.Notify(ch, os.Interrupt, syscall.SIGTERM)
		<-ch
		_ = srv.Close()
	}()

	log.Printf("captcha-solver on %s (scene=%s, headless=%v, page=%s)", cfg.addr, cfg.scene, cfg.headless, cfg.pageURL)
	if err := srv.ListenAndServe(); err != nil && err != http.ErrServerClosed {
		log.Fatalf("serve: %v", err)
	}
}

func (s *solver) handleHealth(w http.ResponseWriter, r *http.Request) {
	s.mu.Lock()
	payload := map[string]any{
		"ok":      true,
		"solves":  s.solves,
		"fails":   s.fails,
		"lastErr": s.lastErr,
	}
	s.mu.Unlock()
	w.Header().Set("Content-Type", "application/json")
	_ = json.NewEncoder(w).Encode(payload)
}

func (s *solver) handleSolve(w http.ResponseWriter, r *http.Request) {
	if r.Method != http.MethodPost {
		http.Error(w, "POST only", http.StatusMethodNotAllowed)
		return
	}
	var req struct {
		SceneID string `json:"sceneId"`
	}
	_ = json.NewDecoder(r.Body).Decode(&req)

	s.mu.Lock()
	defer s.mu.Unlock()

	param, cert, err := s.solve()
	if err != nil {
		s.fails++
		s.lastErr = err.Error()
		log.Printf("solve failed: %v", err)
		w.Header().Set("Content-Type", "application/json")
		w.WriteHeader(http.StatusBadGateway)
		_ = json.NewEncoder(w).Encode(map[string]string{"error": err.Error()})
		return
	}
	s.solves++
	s.lastErr = ""
	log.Printf("solved cert=%s param=%d bytes", cert, len(param))
	w.Header().Set("Content-Type", "application/json")
	_ = json.NewEncoder(w).Encode(map[string]string{"param": param, "cert": cert})
}

// solve drives up to cfg.attempts fresh challenges.
func (s *solver) solve() (string, string, error) {
	var last error
	for attempt := 1; attempt <= s.cfg.attempts; attempt++ {
		param, cert, err := s.attempt()
		if err == nil {
			return param, cert, nil
		}
		last = fmt.Errorf("attempt %d/%d: %w", attempt, s.cfg.attempts, err)
		log.Printf("%v", last)
	}
	if last == nil {
		last = fmt.Errorf("no attempts made")
	}
	return "", "", last
}

func (s *solver) attempt() (string, string, error) {
	ctx, err := s.browser.NewContext(playwright.BrowserNewContextOptions{
		Viewport: &playwright.Size{Width: 1280, Height: 800},
	})
	if err != nil {
		return "", "", fmt.Errorf("new context: %w", err)
	}
	defer ctx.Close()

	page, err := ctx.NewPage()
	if err != nil {
		return "", "", fmt.Errorf("new page: %w", err)
	}
	page.On("console", func(msg playwright.ConsoleMessage) {
		if msg.Type() == "error" || msg.Type() == "warning" {
			log.Printf("console[%s]: %s", msg.Type(), msg.Text())
		}
	})
	page.On("pageerror", func(e error) { log.Printf("pageerror: %v", e) })
	_ = page.BringToFront()

	// Intercept every request so the SDK's verify call is guaranteed to be
	// captured and aborted before it reaches Aliyun (a sent verify burns the
	// certificate). Only post-drag verifies are captured: the SDK first fires
	// a traceless attempt whose body must be allowed through, otherwise the
	// challenge never opens.
	verifyCh := make(chan string, 1)
	var dragging atomic.Bool
	if err := page.Route("**/*", func(route playwright.Route) {
		u := route.Request().URL()
		if strings.Contains(u, "captcha-open-southeast") {
			log.Printf("route: %s (drag=%v)", u, dragging.Load())
		}
		if strings.Contains(u, "captcha-open-southeast") && strings.Contains(u, "verify") && dragging.Load() {
			if pd, perr := route.Request().PostData(); perr == nil {
				select {
				case verifyCh <- pd:
				default:
				}
			}
			_ = route.Abort()
			return
		}
		_ = route.Continue()
	}); err != nil {
		return "", "", fmt.Errorf("route: %w", err)
	}

	if _, err := page.Goto(s.cfg.pageURL, playwright.PageGotoOptions{
		WaitUntil: playwright.WaitUntilStateDomcontentloaded,
		Timeout:   playwright.Float(60000),
	}); err != nil {
		return "", "", fmt.Errorf("goto: %w", err)
	}

	res, err := page.Evaluate(bootJS(s.cfg))
	if err != nil {
		return "", "", fmt.Errorf("boot: %w", err)
	}
	if obj, ok := res.(map[string]any); ok {
		if okv, _ := obj["ok"].(bool); !okv {
			return "", "", fmt.Errorf("boot: %v", obj["error"])
		}
	}

	// Wait for the SDK instance, then drive the trigger exactly the way the
	// site's popupCaptcha integration does: a synthetic click on the hidden
	// button. The init itself also runs a traceless verification, so a pass
	// can arrive before any click and is returned as-is.
	ready, err := page.Evaluate(`(async () => {
        const t0 = Date.now();
        while (Date.now() - t0 < 15000 && !window.__solverReady) {
            await new Promise(r => setTimeout(r, 100));
        }
        return { ready: !!window.__solverReady, err: window.__solverErr || null };
    })()`)
	if m, ok := ready.(map[string]any); ok {
		if v, _ := m["ready"].(bool); !v {
			return "", "", fmt.Errorf("captcha init did not finish (err=%v)", m["err"])
		}
	}

	challengeOpen := func() bool {
		res, err := page.Evaluate(`(() => {
            const w = document.getElementById('aliyunCaptcha-window-popup') || document.getElementById('aliyunCaptcha-window-float');
            const s = document.getElementById('aliyunCaptcha-sliding-slider');
            if (!w || !s) return false;
            if (w.classList.contains('aliyunCaptcha-hidden')) return false;
            const rw = w.getBoundingClientRect(), rs = s.getBoundingClientRect();
            return rw.width > 0 && rw.height > 0 && rs.width > 10;
        })()`)
		if err != nil {
			return false
		}
		v, _ := res.(bool)
		return v
	}

	open := func() error {
		_, err := page.Evaluate(`(() => {
            const b = document.getElementById('__solver_btn');
            if (!b) throw new Error('trigger button missing');
            b.click();
            return true;
        })()`)
		return err
	}

	for i := 0; i < 4; i++ {
		if i > 0 {
			if challengeOpen() {
				break
			}
			if err := open(); err != nil {
				return "", "", fmt.Errorf("open captcha: %w", err)
			}
		}
		res, _ := page.Evaluate(`(async () => {
            const t0 = Date.now();
            while (Date.now() - t0 < 12000) {
                if (window.__solverSuccess) return { param: window.__solverSuccess };
                if (window.__solverErr) return { err: window.__solverErr };
                await new Promise(r => setTimeout(r, 150));
            }
            return { pending: true };
        })()`)
		obj, _ := res.(map[string]any)
		if p, _ := obj["param"].(string); p != "" {
			cert := ""
			if raw, derr := base64.StdEncoding.DecodeString(p); derr == nil {
				var v struct {
					CertifyID string `json:"certifyId"`
				}
				if json.Unmarshal(raw, &v) == nil {
					cert = v.CertifyID
				}
			}
			log.Printf("traceless pass: param %d bytes, cert %q", len(p), cert)
			return p, cert, nil
		}
		if e, _ := obj["err"].(string); e != "" {
			log.Printf("click %d: %s", i+1, e)
			// After a failed traceless attempt the SDK mounts the popup a
			// moment later; give it that moment before deciding to retry.
			for w := 0; w < 10 && !challengeOpen(); w++ {
				time.Sleep(300 * time.Millisecond)
			}
		} else {
			log.Printf("click %d: no verdict in 12s, checking challenge", i+1)
		}
		if challengeOpen() {
			break
		}
	}

	// Give the challenge time to mount, then log what the SDK actually built.
	time.Sleep(2 * time.Second)
	if info, derr := page.Evaluate(`(() => {
        const all = [...document.querySelectorAll('[id]')].map(e => e.id).filter(id => /aliyunCaptcha/i.test(id));
        const out = { ids: all, handle: null, img: null, piece: null };
        const h = document.querySelector('#aliyunCaptcha-sliding-slider');
        if (h) { const b = h.getBoundingClientRect(); out.handle = { w: b.width, h: b.height, x: b.x, y: b.y, vis: getComputedStyle(h).visibility, disp: getComputedStyle(h).display }; }
        const im = document.querySelector('#aliyunCaptcha-img');
        if (im) out.img = { complete: im.complete, nw: im.naturalWidth, w: im.getBoundingClientRect().width };
        const pc = document.querySelector('#aliyunCaptcha-puzzle');
        if (pc) out.piece = { complete: pc.complete, nw: pc.naturalWidth, w: pc.getBoundingClientRect().width };
        const win = document.querySelector('#aliyunCaptcha-window-popup') || document.querySelector('#aliyunCaptcha-window-float');
        out.html = win ? win.outerHTML.slice(0, 2600) : null;
        return out;
    })()`); derr == nil && info != nil {
		log.Printf("challenge DOM: %v", info)
	}

	pz, err := s.waitPuzzle(page, 30*time.Second)
	if err != nil {
		return "", "", err
	}
	log.Printf("puzzle ready: %v", pz)

	if dbg, derr := page.Evaluate(`(() => {
        const r = e => e ? e.getBoundingClientRect() : null;
        const w = document.querySelector('#aliyunCaptcha-window-popup') || document.querySelector('#aliyunCaptcha-window-float');
        const im = document.querySelector('#aliyunCaptcha-img');
        const pc = document.querySelector('#aliyunCaptcha-puzzle');
        const sl = document.querySelector('#aliyunCaptcha-sliding-slider');
        return {
            winCls: w ? String(w.className) : null, winRect: r(w),
            img: im ? { src: (im.src || '').slice(0, 24), nw: im.naturalWidth, w: r(im).width, complete: im.complete } : null,
            pc: pc ? { nw: pc.naturalWidth, w: r(pc).width } : null,
            sl: sl ? { w: r(sl).width, x: r(sl).x, y: r(sl).y } : null,
            nImg: document.querySelectorAll('#aliyunCaptcha-img').length,
            iframes: [...document.querySelectorAll('iframe')].map(f => ({ src: (f.src || '').slice(0, 80), w: r(f).width, h: r(f).height })),
        };
    })()`); derr == nil {
		log.Printf("post-wait: %v", dbg)
	}

	meas, err := page.Evaluate(gapJS)
	if err != nil {
		return "", "", fmt.Errorf("measure: %w", err)
	}
	log.Printf("measure raw: %v", meas)
	m, ok := meas.(map[string]any)
	if !ok {
		return "", "", fmt.Errorf("measure: unexpected %T", meas)
	}
	if e, _ := m["error"].(string); e != "" {
		return "", "", fmt.Errorf("measure: %s", e)
	}
	// The SDK can swap the background image right after the challenge opens
	// (a refresh triggered by the failed traceless attempts), leaving an
	// empty src for a moment. Poll until both widths are real.
	for i := 0; i < 20 && (num(m, "W") <= 0 || num(m, "renderedW") <= 0); i++ {
		time.Sleep(300 * time.Millisecond)
		meas, err = page.Evaluate(gapJS)
		if err != nil {
			return "", "", fmt.Errorf("measure: %w", err)
		}
		if v, ok := meas.(map[string]any); ok {
			m = v
		}
		if e, _ := m["error"].(string); e != "" {
			return "", "", fmt.Errorf("measure: %s", e)
		}
	}
	if num(m, "W") <= 0 || num(m, "renderedW") <= 0 {
		return "", "", fmt.Errorf("measure: bad widths nat=%v(%T) css=%v(%T)", m["W"], m["W"], m["renderedW"], m["renderedW"])
	}

	peakL := num(m, "peakL")
	peakR := num(m, "peakR")
	px0 := num(m, "px0")
	natW := num(m, "W")
	rendW := num(m, "renderedW")
	hx := num(m, "handleX")
	hy := num(m, "handleY")
	cert, _ := m["cert"].(string)
	if natW <= 0 || rendW <= 0 {
		return "", "", fmt.Errorf("measure: bad widths nat=%v css=%v", natW, rendW)
	}

	// Use the left boundary peak as the hole edge. The right peak is logged
	// for diagnosis but is not allowed to move the offset: on real challenges
	// it regularly locks onto unrelated background texture tens of pixels away.
	holeLeftImg := peakL
	log.Printf("measure peaks: L=%.0f R=%.0f", peakL, peakR)
	scale := rendW / natW
	target := (holeLeftImg - px0) * scale
	h := captchasolve.PointerDelta(target)
	if h <= 0 || h > 290 {
		return "", "", fmt.Errorf("measure: implausible pointer delta %d (target %.2f)", h, target)
	}
	xpos := captchasolve.DragXPos(float64(h))

	if err := drag(page, hx, hy, h, &dragging); err != nil {
		return "", "", fmt.Errorf("drag: %w", err)
	}

	var body string
	select {
	case body = <-verifyCh:
	case <-time.After(20 * time.Second):
		return "", "", fmt.Errorf("SDK did not send VerifyCaptchaV3")
	}

	cvp, err := parseCV(body)
	if err != nil {
		return "", "", err
	}
	if cvp.CertifyID == "" {
		cvp.CertifyID = cert
	}

	newInner, err := captchasolve.Unpack(cvp.Data)
	if err != nil {
		return "", "", fmt.Errorf("unpack sdk payload: %w", err)
	}
	tmpl, err := captchasolve.Unpack(captchasolve.TemplateData())
	if err != nil {
		return "", "", fmt.Errorf("unpack template: %w", err)
	}
	out, err := captchasolve.Transplant(newInner, float64(h), xpos, hx, hy, tmpl)
	if err != nil {
		return "", "", fmt.Errorf("transplant: %w", err)
	}
	data, err := captchasolve.Pack(out)
	if err != nil {
		return "", "", fmt.Errorf("pack: %w", err)
	}

	param, err := zbridge.VerifyCVParam(cvp.CertifyID, data, cvp.DeviceToken)
	if err != nil {
		return "", "", fmt.Errorf("verify: %w", err)
	}
	if param == "" {
		return "", "", fmt.Errorf("verify returned no token (cert %s, slide %d, xPos %d)", cvp.CertifyID, h, xpos)
	}
	return param, cvp.CertifyID, nil
}

func (s *solver) waitPuzzle(page playwright.Page, timeout time.Duration) (map[string]any, error) {
	deadline := time.Now().Add(timeout)
	for time.Now().Before(deadline) {
		res, err := page.Evaluate(`(() => {
            const q = s => document.querySelector(s);
            const h = q('#aliyunCaptcha-sliding-slider');
            const im = q('#aliyunCaptcha-img'), pc = q('#aliyunCaptcha-puzzle');
            const b = h && h.getBoundingClientRect();
            const ready = !!(b && b.width > 10 && im && im.complete && im.naturalWidth > 0 && pc && pc.complete);
            return { ready: ready, cert: (q('#aliyunCaptcha-certifyId') || {}).textContent || null,
                     handle: b ? { x: b.x + b.width / 2, y: b.y + b.height / 2 } : null };
        })()`)
		if err == nil {
			if m, ok := res.(map[string]any); ok {
				if v, _ := m["ready"].(bool); v {
					return m, nil
				}
			}
		}
		time.Sleep(250 * time.Millisecond)
	}
	dbg, derr := page.Evaluate(`(() => {
        const win = document.querySelector('#aliyunCaptcha-window-popup') || document.querySelector('#aliyunCaptcha-window-float');
        const ids = [...document.querySelectorAll('[id^=aliyunCaptcha]')].map(e => e.id);
        let kids = [];
        if (win) kids = [...win.querySelectorAll('*')].slice(0, 50).map(e => ({
            tag: e.tagName, id: e.id,
            cls: e.className && String(e.className).slice(0, 70),
            w: Math.round(e.getBoundingClientRect().width),
            h: Math.round(e.getBoundingClientRect().height),
        }));
        return { ready: !!window.__solverReady, err: window.__solverErr || null,
                 ids: ids.slice(0, 40), kids: kids,
                 html: win ? win.outerHTML.slice(0, 4000) : null };
    })()`)
	if derr != nil {
		return nil, fmt.Errorf("puzzle did not become ready in %s (debug failed: %v)", timeout, derr)
	}
	return nil, fmt.Errorf("puzzle did not become ready in %s: %v", timeout, dbg)
}

func drag(page playwright.Page, hx, hy float64, h int, dragging *atomic.Bool) error {
	m := page.Mouse()
	if err := m.Move(hx, hy); err != nil {
		return err
	}
	if err := m.Down(); err != nil {
		return err
	}
	const steps = 28
	for i := 1; i <= steps; i++ {
		frac := float64(i) / float64(steps)
		x := hx + float64(h)*frac
		y := hy + 0.6*math.Sin(frac*math.Pi*2)
		if err := m.Move(x, y, playwright.MouseMoveOptions{Steps: playwright.Int(1)}); err != nil {
			return err
		}
		time.Sleep(9 * time.Millisecond)
	}
	if err := m.Move(hx+float64(h), hy); err != nil {
		return err
	}
	time.Sleep(60 * time.Millisecond)
	dragging.Store(true) // from here on, capture and abort the SDK verify
	return m.Up()
}

type cvp struct {
	SceneID     string `json:"sceneId"`
	CertifyID   string `json:"certifyId"`
	DeviceToken string `json:"deviceToken"`
	Data        string `json:"data"`
}

func parseCV(body string) (*cvp, error) {
	vals, err := url.ParseQuery(body)
	if err != nil {
		return nil, fmt.Errorf("verify body: %w", err)
	}
	raw := vals.Get("CaptchaVerifyParam")
	if raw == "" {
		return nil, fmt.Errorf("verify body has no CaptchaVerifyParam (%d bytes)", len(body))
	}
	var c cvp
	if err := json.Unmarshal([]byte(raw), &c); err != nil {
		return nil, fmt.Errorf("CaptchaVerifyParam JSON: %w", err)
	}
	if c.Data == "" || c.DeviceToken == "" {
		return nil, fmt.Errorf("capture missing data/deviceToken")
	}
	return &c, nil
}

func num(m map[string]any, key string) float64 {
	switch v := m[key].(type) {
	case float64:
		return v
	case float32:
		return float64(v)
	case int:
		return float64(v)
	case int64:
		return float64(v)
	case json.Number:
		f, _ := v.Float64()
		return f
	case string:
		f, _ := strconv.ParseFloat(v, 64)
		return f
	}
	return 0
}

func bootJS(cfg config) string {
	return fmt.Sprintf(`(async () => {
        const sleep = ms => new Promise(r => setTimeout(r, ms));
        try {
            window.AliyunCaptchaConfig = window.AliyunCaptchaConfig || { region: '%s', prefix: '%s' };
            if (!window.initAliyunCaptcha) {
                let lastErr = null;
                for (let i = 0; i < 3 && !window.initAliyunCaptcha; i++) {
                    try {
                        await new Promise((res, rej) => {
                            const s = document.createElement('script');
                            s.src = '%s';
                            s.onload = res;
                            s.onerror = () => { s.remove(); rej(new Error('captcha sdk load failed')); };
                            document.head.appendChild(s);
                        });
                    } catch (e) {
                        lastErr = e;
                        await sleep(400);
                    }
                }
                if (!window.initAliyunCaptcha) throw lastErr || new Error('captcha sdk unavailable');
            }
            // Same shape as the site's popupCaptcha integration: the mount and
            // trigger are deliberately off-screen and 1x1. The SDK opens the
            // challenge from a synthetic click on the trigger (the site's own
            // getVerifyParam does exactly that), so no mouse click is needed.
            let el = document.getElementById('__solver_el');
            if (!el) {
                el = document.createElement('div');
                el.id = '__solver_el';
                el.style.cssText = 'position:absolute;left:-99999px;top:-99999px;width:0;height:0;overflow:hidden;pointer-events:none;';
                document.body.appendChild(el);
            }
            let btn = document.getElementById('__solver_btn');
            if (!btn) {
                btn = document.createElement('button');
                btn.id = '__solver_btn';
                btn.type = 'button';
                btn.setAttribute('aria-hidden', 'true');
                btn.tabIndex = -1;
                btn.style.cssText = 'position:absolute;left:-99999px;top:-99999px;width:1px;height:1px;opacity:0;';
                document.body.appendChild(btn);
            }
            window.__solverReady = false;
            window.__solverSuccess = null;
            window.__solverErr = null;
            window.initAliyunCaptcha({
                SceneId: '%s',
                mode: 'popup',
                element: '#__solver_el',
                button: '#__solver_btn',
                captchaLogoImg: 'https://z-cdn.chatglm.cn/z-ai/static/logo.svg',
                upLang: %s,
                language: 'en',
                timeout: 10000,
                delayBeforeSuccess: false,
                success: function (p) { window.__solverSuccess = (typeof p === 'string') ? p : JSON.stringify(p); },
                fail: function (e) { window.__solverErr = 'fail:' + JSON.stringify(e); },
                onError: function (e) { window.__solverErr = 'onError:' + JSON.stringify(e); },
                onClose: function () {},
                getInstance: function (i) { window.__solver_inst = i; window.__solverReady = true; },
            });
            return { ok: true };
        } catch (e) {
            return { ok: false, error: String((e && e.stack) || e) };
        }
    })()`, cfg.region, cfg.prefix, sdkURL, cfg.scene, siteUpLang)
}

// siteUpLang is the exact `myLang` object the Z.AI frontend passes as
// `upLang`; the SDK rejects the option when its shape is wrong and then never
// renders the challenge window.
const siteUpLang = `{
    cn: {START_VERIFY: "点击开始验证", POPUP_TITLE: "请完成安全验证", SLIDE_TIP: "请按住滑块，拖动到最右边", CHECK_BOX_TIP: "确认您不是机器人", PUZZLE_TIP: "请拖动滑块完成拼图", INPAINTING_TIP: "请拖动滑块还原完整图片", VERIFYING: "验证中...", SUCCESS: "滑动成功!", SLIDE_FAIL: "验证失败，请刷新重试", CAPTCHA_FAIL: "验证失败，请重试!", CONGESTION: "前方拥堵，请刷新重试", CAPTCHA_COMPLETED: "滑动完成", FINISH_CAPTCHA: "请先完成验证！"},
    en: {START_VERIFY: "Click to start verification", POPUP_TITLE: "Please complete security verification", SLIDE_TIP: "Please drag slider right", CHECK_BOX_TIP: "Confirm you are not a robot", PUZZLE_TIP: "Please drag the slider to complete the puzzle", INPAINTING_TIP: "Please drag the slider to restore the complete image", VERIFYING: "Verifying...", SUCCESS: "Slide successful!", SLIDE_FAIL: "Verification failed, please refresh and try again", CAPTCHA_FAIL: "Verification failed, please try again!", CONGESTION: "Network congestion, please refresh and try again", CAPTCHA_COMPLETED: "Slide completed", FINISH_CAPTCHA: "Please complete verification first!"}
}`

// gapJS measures the hole from the real background/piece images. The left and
// right piece boundaries vote for the same offset: at the correct displacement
// the outside of each boundary lands on the hole edge, whose interior is
// brighter. The two peaks differ by the hole's antialias inset, so their
// average is the edge.
const gapJS = `(async () => {
    const q = s => document.querySelector(s);
    const bg = q('#aliyunCaptcha-img'), pc = q('#aliyunCaptcha-puzzle');
    if (!bg || !pc || !bg.complete || !pc.complete) return { error: 'images not ready' };
    const toBlob = url => new Promise(res => {
        const x = new XMLHttpRequest();
        x.open('GET', url); x.responseType = 'blob';
        x.onload = () => res(x.response); x.onerror = () => res(null); x.send();
    });
    const load = b => new Promise(res => {
        const u = URL.createObjectURL(b); const i = new Image();
        i.onload = () => res(i); i.onerror = () => res(null); i.src = u;
    });
    const bi = await load(await toBlob(bg.src)), pi = await load(await toBlob(pc.src));
    if (!bi || !pi) return { error: 'image decode failed' };
    const W = bi.naturalWidth, H = bi.naturalHeight, PW = pi.naturalWidth;
    const c1 = document.createElement('canvas'); c1.width = W; c1.height = H;
    const g1 = c1.getContext('2d'); g1.drawImage(bi, 0, 0, W, H);
    let d1;
    try { d1 = g1.getImageData(0, 0, W, H).data; } catch (e) { return { error: 'background is cross-origin' }; }
    const c2 = document.createElement('canvas'); c2.width = PW; c2.height = H;
    const g2 = c2.getContext('2d'); g2.drawImage(pi, 0, 0, PW, H);
    const d2 = g2.getImageData(0, 0, PW, H).data;
    const al = (x, y) => d2[(y * PW + x) * 4 + 3];
    const G = i => (d1[i * 4] + d1[i * 4 + 1] + d1[i * 4 + 2]) / 3;
    const left = [], right = [];
    let px0 = PW, px1 = 0, py0 = H, py1 = 0, pn = 0;
    for (let y = 0; y < H; y++) for (let x = 0; x < PW; x++) if (al(x, y) > 160) {
        pn++;
        if (x < px0) px0 = x; if (x > px1) px1 = x;
        if (y < py0) py0 = y; if (y > py1) py1 = y;
    }
    if (pn < 100) return { error: 'piece mask empty' };
    for (let y = 0; y < H; y++) {
        let lx = -1, rx = -1;
        for (let x = 0; x < PW; x++) if (al(x, y) > 160) { lx = x; break; }
        for (let x = PW - 1; x >= 0; x--) if (al(x, y) > 160) { rx = x; break; }
        if (lx > 0 && al(lx - 1, y) < 80) left.push([lx, y]);
        if (rx >= 0 && rx < PW - 1 && al(rx + 1, y) < 80) right.push([rx, y]);
    }
    if (left.length < 8 || right.length < 8) return { error: 'piece boundary too thin' };
    const sL = [], sR = [];
    for (let s = 0; s <= W - PW; s++) {
        let a = 0, b = 0;
        for (const [x, y] of left) a += G(y * W + s + x) - G(y * W + s + x - 1);
        for (const [x, y] of right) b += G(y * W + s + x) - G(y * W + s + x + 1);
        sL.push(a / left.length); sR.push(b / right.length);
    }
    let peakL = 0, peakR = 0;
    for (let i = 1; i < sL.length; i++) { if (sL[i] > sL[peakL]) peakL = i; if (sR[i] > sR[peakR]) peakR = i; }
    // The left boundary is the stable vote: across every observed challenge
    // (including the ones where the right peak landed on background texture
    // tens of pixels away) its maximum tracked the hole edge. The right score
    // remains in the log as a sanity signal, not as an input to the offset.
    const hb = q('#aliyunCaptcha-sliding-slider');
    const hr = hb ? hb.getBoundingClientRect() : null;
    return { W: W, H: H, PW: PW, pn: pn, px0: px0, px1: px1,
             peakL: peakL, peakR: peakR,
             leftN: left.length, rightN: right.length,
             renderedW: bg.getBoundingClientRect().width,
             handleX: hr ? hr.x + hr.width / 2 : 0, handleY: hr ? hr.y + hr.height / 2 : 0,
             cert: (q('#aliyunCaptcha-certifyId') || {}).textContent || null };
})()`
