package zbridge

import (
	"bytes"
	"encoding/json"
	"errors"
	"fmt"
	"io"
	"net/http"
	"strings"
	"time"
)

type solverRequest struct {
	SceneID string `json:"sceneId"`
}

type solverResponse struct {
	Param string `json:"param"`
	Cert  string `json:"cert"`
	Error string `json:"error"`
}

// solverParam asks the browser solver for one fresh captcha_verify_param.
func solverParam(base string) (string, error) {
	base = strings.TrimRight(base, "/")
	if base == "" {
		return "", errors.New("CAPTCHA_SOLVER_URL is empty")
	}
	body, err := json.Marshal(solverRequest{SceneID: sceneID})
	if err != nil {
		return "", err
	}
	req, err := http.NewRequest(http.MethodPost, base+"/solve", bytes.NewReader(body))
	if err != nil {
		return "", err
	}
	req.Header.Set("Content-Type", "application/json")

	client := &http.Client{Timeout: 85 * time.Second}
	resp, err := client.Do(req)
	if err != nil {
		return "", fmt.Errorf("solver request: %w", err)
	}
	defer resp.Body.Close()

	raw, err := io.ReadAll(io.LimitReader(resp.Body, 64<<10))
	if err != nil {
		return "", fmt.Errorf("solver read: %w", err)
	}
	var out solverResponse
	if err := json.Unmarshal(raw, &out); err != nil {
		return "", fmt.Errorf("solver response (%d): %s", resp.StatusCode, strings.TrimSpace(string(raw)))
	}
	if resp.StatusCode != http.StatusOK || out.Param == "" {
		msg := out.Error
		if msg == "" {
			msg = strings.TrimSpace(string(raw))
		}
		return "", fmt.Errorf("solver failed (%d): %s", resp.StatusCode, msg)
	}
	return out.Param, nil
}
