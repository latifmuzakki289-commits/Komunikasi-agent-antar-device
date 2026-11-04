package zbridge

import (
	"encoding/json"
	"strings"
	"testing"
)

// TestSanitizeLeakedToolBlocksRemovesBlock covers the "output kacau" root cause:
// a previous turn's tool call rendered into the transcript as plain text inside
// a <glm_block ...> tag. Replaying that verbatim teaches the model to imitate
// the malformed block, so it must never reach the upstream prompt.
func TestSanitizeLeakedToolBlocksRemovesBlock(t *testing.T) {
	cases := []struct {
		name string
		in   string
		want string
	}{
		{
			name: "simple block",
			in:   "Saya cek latency.\n\n<glm_block tool_call_name=\"bash\">{\"data\":{\"arguments\":\"x\"}}</glm_block>\n\nSelesai.",
			want: "Saya cek latency.\n\n" + leakedBlockSummary + "\n\nSelesai.",
		},
		{
			name: "dsh_block variant",
			in:   "a <dsh_block tool_call_name=\"bash\">{}</dsh_block> b",
			want: "a " + leakedBlockSummary + " b",
		},
		{
			name: "two blocks",
			in:   "<glm_block a>1</glm_block> mid <glm_block b>2</glm_block>",
			want: leakedBlockSummary + " mid " + leakedBlockSummary,
		},
		{
			name: "unterminated block drops the tail",
			in:   "before <glm_block tool_call_name=\"bash\">{\"data\":{\"name\":\"bas",
			want: "before " + leakedBlockSummary,
		},
		{
			name: "plain text untouched",
			in:   "tidak ada blok di sini",
			want: "tidak ada blok di sini",
		},
	}
	for _, tc := range cases {
		t.Run(tc.name, func(t *testing.T) {
			if got := sanitizeLeakedToolBlocks(tc.in); got != tc.want {
				t.Errorf("sanitizeLeakedToolBlocks()\n got: %q\nwant: %q", got, tc.want)
			}
		})
	}
}

// TestSanitizeIsIdempotent guards the render path, which may run repeatedly.
func TestSanitizeIsIdempotent(t *testing.T) {
	in := "x <glm_block tool_call_name=\"bash\">{}</glm_block> y"
	once := sanitizeLeakedToolBlocks(in)
	if twice := sanitizeLeakedToolBlocks(once); twice != once {
		t.Errorf("not idempotent: %q -> %q", once, twice)
	}
}

func TestStripLeakedToolBlocksRemovesVisibleBlock(t *testing.T) {
	in := "awal <glm_block tool_call_name=\"WebSearch\">{\"data\":{}}</glm_block> akhir"
	want := "awal  akhir"
	if got := stripLeakedToolBlocks(in); got != want {
		t.Errorf("stripLeakedToolBlocks()\\n got: %q\\nwant: %q", got, want)
	}
}

func TestLeakedToolBlockFilterHandlesChunkBoundaries(t *testing.T) {
	f := newLeakedToolBlockFilter()
	var got strings.Builder
	for _, chunk := range []string{
		"awal <glm_", "block tool_call_name=\"WebSearch\">{\"data\":",
		"{}}<", "/glm_block> akhir",
	} {
		got.WriteString(f.feed(chunk))
	}
	got.WriteString(f.finish())
	if want := "awal  akhir"; got.String() != want {
		t.Errorf("leakedToolBlockFilter()\\n got: %q\\nwant: %q", got.String(), want)
	}
}

// TestAgentPromptDropsLeakedBlocks proves the sanitizer is actually wired into
// the prompt builder: a contaminated assistant turn must not appear verbatim.
func TestAgentPromptDropsLeakedBlocks(t *testing.T) {
	raw, err := json.Marshal([]map[string]interface{}{
		{"role": "user", "content": "halo"},
		{"role": "assistant", "content": "<glm_block tool_call_name=\"bash\">{\"data\":{\"name\":\"bas</glm_block> jawaban"},
		{"role": "user", "content": "lanjut"},
	})
	if err != nil {
		t.Fatal(err)
	}
	var msgs []agentMessage
	if err := json.Unmarshal(raw, &msgs); err != nil {
		t.Fatal(err)
	}
	prompt := buildAgentPrompt(msgs, nil)
	if strings.Contains(prompt, "glm_block") {
		t.Error("prompt still contains leaked glm_block text")
	}
	if !strings.Contains(prompt, leakedBlockSummary) {
		t.Error("prompt should carry the neutral placeholder instead")
	}
}

func TestUpstreamSSERemovesLeakedToolBlocks(t *testing.T) {
	oldHoldback := config.StreamHoldback
	config.StreamHoldback = 0
	defer func() { config.StreamHoldback = oldHoldback }()

	body := strings.Join([]string{
		sseEvent(`{"data":{"delta_content":"before <glm_"}}`),
		sseEvent(`{"data":{"delta_content":"block tool_call_name=\"WebSearch\">{\"data\":{}}"}}`),
		sseEvent(`{"data":{"delta_content":"</glm_block> after"}}`),
		sseEvent(`{"data":{"phase":"done"}}`),
		sseEvent(`[DONE]`),
	}, "")
	results := runSSEParser(t, body)
	clientText, _ := replayClientView(results)
	if strings.Contains(clientText, "glm_block") || strings.Contains(clientText, "WebSearch") {
		t.Fatalf("malformed native tool block leaked through upstream SSE: %q", clientText)
	}
	if clientText != "before  after" {
		t.Fatalf("client text = %q, want %q", clientText, "before  after")
	}
}

func TestUpstreamSSERemovesLeakedBlockFromReasoning(t *testing.T) {
	oldHoldback := config.StreamHoldback
	config.StreamHoldback = 0
	defer func() { config.StreamHoldback = oldHoldback }()

	body := strings.Join([]string{
		sseEvent(`{"data":{"delta_content":"<details>think\n<glm_block tool_call_name=\"WebSearch\">{}\n</glm_block>\n</details>answer"}}`),
		sseEvent(`{"data":{"phase":"done"}}`),
		sseEvent(`[DONE]`),
	}, "")
	results := runSSEParser(t, body)
	for _, result := range results {
		if strings.Contains(result.Reasoning, "glm_block") || strings.Contains(result.Reasoning, "WebSearch") {
			t.Fatalf("malformed block leaked through reasoning channel: %q", result.Reasoning)
		}
	}
}
