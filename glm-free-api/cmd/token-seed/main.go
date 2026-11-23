// Command token-seed mints a fresh pool of Aliyun device tokens from one or
// more already-trusted tokens.
//
// A device token is
//
//	base64("SG_WEB#<session>#<w>#<tC>#<p>")
//
// where p is the MD5 of the other fields plus a fixed salt.  Aliyun's risk
// engine keys trust on the (session, w) pair — the encrypted device payload —
// and only requires every token to be single-use and unique.  tC (the gather
// cost) is the discriminator, so a pool of trusted tokens can be expanded
// arbitrarily by re-pairing their payloads with unused tC values.
//
// This is the fallback refill path: harvesting a *new* trusted session fails
// with VerifyCode F001 (risk policy) on this host, while tokens seeded from a
// session minted before the policy change still verify with T001.
package main

import (
	"bufio"
	"crypto/md5"
	"database/sql"
	"encoding/base64"
	"encoding/hex"
	"flag"
	"fmt"
	"os"
	"sort"
	"strconv"
	"strings"

	_ "modernc.org/sqlite"
)

const checksumSalt = "daye,raolewoba!"

type seed struct {
	tF string
	q  string
	w  string
}

func decode(tok string) (seed, string, bool) {
	raw, err := base64.StdEncoding.DecodeString(strings.TrimSpace(tok))
	if err != nil {
		return seed{}, "", false
	}
	parts := strings.Split(string(raw), "#")
	if len(parts) != 5 {
		return seed{}, "", false
	}
	if checksum(parts[0], parts[1], parts[2], parts[3]) != parts[4] {
		return seed{}, "", false
	}
	return seed{tF: parts[0], q: parts[1], w: parts[2]}, parts[3], true
}

func checksum(tF, q, w, tC string) string {
	sum := md5.Sum([]byte(tF + "#" + q + "#" + w + "#" + tC + "#" + checksumSalt))
	return hex.EncodeToString(sum[:])
}

func encode(s seed, tC string) string {
	return base64.StdEncoding.EncodeToString(
		[]byte(s.tF + "#" + s.q + "#" + s.w + "#" + tC + "#" + checksum(s.tF, s.q, s.w, tC)))
}

// readTokens accepts either a tokens.sqlite (table tokens(token TEXT)) or a
// plain text file with one token per line.
func readTokens(path string) ([]string, error) {
	f, err := os.Open(path)
	if err != nil {
		return nil, err
	}
	head := make([]byte, 16)
	n, _ := f.Read(head)
	_ = f.Close()
	if n == 16 && string(head) == "SQLite format 3\x00" {
		db, err := sql.Open("sqlite", "file:"+path+"?mode=ro")
		if err != nil {
			return nil, err
		}
		defer db.Close()
		rows, err := db.Query("SELECT token FROM tokens")
		if err != nil {
			return nil, err
		}
		defer rows.Close()
		var out []string
		for rows.Next() {
			var t string
			if err := rows.Scan(&t); err != nil {
				return nil, err
			}
			out = append(out, t)
		}
		return out, rows.Err()
	}
	f, err = os.Open(path)
	if err != nil {
		return nil, err
	}
	defer f.Close()
	var out []string
	sc := bufio.NewScanner(f)
	sc.Buffer(make([]byte, 64*1024), 2*1024*1024)
	for sc.Scan() {
		if line := strings.TrimSpace(sc.Text()); line != "" && !strings.HasPrefix(line, "#") {
			out = append(out, line)
		}
	}
	return out, sc.Err()
}

func main() {
	fromFlag := flag.String("from", "", "seed source: trusted tokens.sqlite or a text file of tokens")
	outFlag := flag.String("out", "tokens.sqlite", "output database")
	countFlag := flag.Int("count", 1200, "number of tokens to mint")
	minTCFlag := flag.Int("min-tc", 600, "lowest gather-cost value to use")
	maxTCFlag := flag.Int("max-tc", 1399, "highest gather-cost value to use")
	flag.Parse()

	if *fromFlag == "" {
		fmt.Fprintln(os.Stderr, "token-seed: -from is required")
		os.Exit(2)
	}

	raw, err := readTokens(*fromFlag)
	if err != nil {
		fmt.Fprintln(os.Stderr, "token-seed: read seeds:", err)
		os.Exit(1)
	}

	seen := make(map[string]bool)
	usedTC := make(map[int]bool)
	var seeds []seed
	for _, tok := range raw {
		s, tC, ok := decode(tok)
		if !ok {
			continue
		}
		key := s.q + "\x00" + s.w
		if seen[key] {
			continue
		}
		seen[key] = true
		seeds = append(seeds, s)
		if v, err := strconv.Atoi(tC); err == nil {
			usedTC[v] = true
		}
	}
	if len(seeds) == 0 {
		fmt.Fprintln(os.Stderr, "token-seed: no valid seed tokens in", *fromFlag)
		os.Exit(1)
	}
	sort.Slice(seeds, func(i, j int) bool { return seeds[i].w < seeds[j].w })

	var tcs []string
	for v := *minTCFlag; v <= *maxTCFlag; v++ {
		if !usedTC[v] {
			tcs = append(tcs, strconv.Itoa(v))
		}
	}
	if len(tcs) == 0 {
		fmt.Fprintln(os.Stderr, "token-seed: empty gather-cost range")
		os.Exit(1)
	}

	capacity := len(seeds) * len(tcs)
	if *countFlag > capacity {
		fmt.Fprintf(os.Stderr, "token-seed: %d tokens requested, capacity is %d (%d payloads x %d gather costs)\n",
			*countFlag, capacity, len(seeds), len(tcs))
		os.Exit(1)
	}

	_ = os.Remove(*outFlag)
	db, err := sql.Open("sqlite", *outFlag)
	if err != nil {
		fmt.Fprintln(os.Stderr, "token-seed: open out:", err)
		os.Exit(1)
	}
	defer db.Close()
	if _, err := db.Exec("CREATE TABLE tokens (id INTEGER PRIMARY KEY AUTOINCREMENT, token TEXT NOT NULL)"); err != nil {
		fmt.Fprintln(os.Stderr, "token-seed: create table:", err)
		os.Exit(1)
	}
	tx, err := db.Begin()
	if err != nil {
		fmt.Fprintln(os.Stderr, "token-seed: begin:", err)
		os.Exit(1)
	}
	stmt, err := tx.Prepare("INSERT INTO tokens (token) VALUES (?)")
	if err != nil {
		fmt.Fprintln(os.Stderr, "token-seed: prepare:", err)
		os.Exit(1)
	}
	made := 0
	for round := 0; made < *countFlag; round++ {
		tC := tcs[round%len(tcs)]
		for _, s := range seeds {
			if made >= *countFlag {
				break
			}
			if _, err := stmt.Exec(encode(s, tC)); err != nil {
				fmt.Fprintln(os.Stderr, "token-seed: insert:", err)
				os.Exit(1)
			}
			made++
		}
	}
	_ = stmt.Close()
	if err := tx.Commit(); err != nil {
		fmt.Fprintln(os.Stderr, "token-seed: commit:", err)
		os.Exit(1)
	}
	fmt.Printf("token-seed: minted %d tokens from %d payloads (session %s, gather costs %s..%s)\n",
		*countFlag, len(seeds), shortSession(seeds[0].q), tcs[0], tcs[len(tcs)-1])
}

func shortSession(q string) string {
	if i := strings.Index(q, "-h-"); i >= 0 && len(q) > i+3 {
		return q[:min(i+16, len(q))]
	}
	return q
}
