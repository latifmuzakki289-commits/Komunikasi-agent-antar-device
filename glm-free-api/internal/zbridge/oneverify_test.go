package zbridge

import (
	"fmt"
	"os"
	"testing"
)

// Diagnostic: verify exactly one device token against Aliyun VerifyCaptchaV3
// and print the raw verdict. Does not touch tokens.sqlite.
func TestOneVerify(t *testing.T) {
	tok := os.Getenv("ONE_TOKEN")
	if tok == "" {
		t.Skip("ONE_TOKEN not set")
	}
	certifyID, err := initCaptcha()
	if err != nil {
		t.Fatalf("initCaptcha: %v", err)
	}
	fmt.Println("CERTIFY:", certifyID)

	argValue := generateArg(certifyID)
	ct := currentTimeMillis()
	track := Track{
		TrackList:      TrackList{StartTime: ct},
		TrackStartTime: ct,
		VerifyTime:     ct + 300,
		Arg:            argValue,
	}
	jsonBytes, err := jsonMarshal(track)
	if err != nil {
		t.Fatalf("marshal: %v", err)
	}
	h := aliHash(string(jsonBytes), "0000")
	compressed := zlibCompress([]byte(h + string(jsonBytes)))
	finalVal := encrypt([]byte(base64Encode(compressed)))

	cvpJSON, err := jsonMarshal(CVP{
		CertifyID:   certifyID,
		Data:        finalVal,
		DeviceToken: tok,
		SceneID:     sceneID,
	})
	if err != nil {
		t.Fatalf("cvp: %v", err)
	}
	params := map[string]string{
		"AccessKeyId":        accessKey,
		"Action":             "VerifyCaptchaV3",
		"Format":             "JSON",
		"SignatureMethod":    "HMAC-SHA1",
		"SignatureVersion":   "1.0",
		"Timestamp":          getTimestampUTC(),
		"Version":            "2023-03-05",
		"SceneId":            sceneID,
		"CertifyId":          certifyID,
		"CaptchaVerifyParam": string(cvpJSON),
		"SignatureNonce":     generateUUID(),
	}
	params["Signature"] = generateSignature(params, secretKey)
	body := buildQueryString(params)
	resp, err := httpPost("https://no8xfe-verify.captcha-open-southeast.aliyuncs.com/", body, map[string]string{"Referer": ""})
	fmt.Println("RAW:", resp)
	if err != nil {
		fmt.Println("ERR:", err)
	}
}
