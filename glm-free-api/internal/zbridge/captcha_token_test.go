package zbridge

import (
	"fmt"
	"sync"
	"testing"
)

func TestGetNextTokenAtomicallyClaimsUniqueRows(t *testing.T) {
	const total = 128
	attachTestDB(t, total)

	tokens := make(chan string, total)
	errs := make(chan error, total)
	var wg sync.WaitGroup
	for i := 0; i < total; i++ {
		wg.Add(1)
		go func() {
			defer wg.Done()
			token, ok := getNextToken()
			if !ok {
				errs <- fmt.Errorf("token claim failed")
				return
			}
			tokens <- token
		}()
	}
	wg.Wait()
	close(tokens)
	close(errs)
	for err := range errs {
		t.Error(err)
	}

	seen := make(map[string]struct{}, total)
	for token := range tokens {
		if _, duplicate := seen[token]; duplicate {
			t.Errorf("same single-use token claimed more than once: %q", token)
		}
		seen[token] = struct{}{}
	}
	if len(seen) != total {
		t.Fatalf("claimed %d unique tokens, want %d", len(seen), total)
	}

	h, err := activeHandle()
	if err != nil {
		t.Fatal(err)
	}
	var remaining int
	if err := h.db.QueryRow("SELECT COUNT(*) FROM tokens").Scan(&remaining); err != nil {
		t.Fatal(err)
	}
	if remaining != 0 {
		t.Fatalf("remaining token rows = %d, want 0", remaining)
	}
	if _, ok := getNextToken(); ok {
		t.Fatal("claim on empty token table unexpectedly succeeded")
	}
}
