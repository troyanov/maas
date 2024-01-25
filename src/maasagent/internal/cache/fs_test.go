package cache

import (
	"bytes"
	"errors"
	"fmt"
	"os"
	"path"
	"strconv"
	"strings"
	"testing"
	"time"

	"github.com/stretchr/testify/assert"
)

func TestNewFSCache(t *testing.T) {
	type in struct {
		maxSize int64
		path    string
	}

	testcases := map[string]struct {
		in  in
		out *FSCache
		err error
	}{
		"zero max cache size": {
			in:  in{maxSize: 0, path: ""},
			out: nil,
			err: ErrPositiveMaxCacheSize,
		},
		"negative max cache size": {
			in:  in{maxSize: -1, path: ""},
			out: nil,
			err: ErrPositiveMaxCacheSize,
		},
		"no path provided": {
			in:  in{maxSize: 1, path: ""},
			out: nil,
			err: ErrMissingCachePath,
		},
		"non existing directory": {
			in: in{maxSize: 1, path: func() string {
				p := fmt.Sprintf("%s-%d", t.Name(), time.Now().Unix())
				return path.Join(os.TempDir(), p)
			}()},
			out: nil,
			err: nil,
		},
		"existing empty directory": {
			in:  in{maxSize: 1, path: t.TempDir()},
			out: nil,
			err: nil,
		},
		"existing directory with files larger than cache size": {
			in: in{
				maxSize: 1,
				path: func() string {
					dir := t.TempDir()
					file, err := os.Create(path.Join(dir, "item"))
					if err != nil {
						t.Fatal(err)
					}
					defer file.Close()

					_, err = file.Write(make([]byte, 2))
					if err != nil {
						t.Fatal(err)
					}

					return dir
				}(),
			},
			out: nil,
			err: ErrCacheSizeExceeded,
		},
		"existing directory with files that fit cache size": {
			in: in{
				maxSize: 2,
				path: func() string {
					dir := t.TempDir()
					file, err := os.Create(path.Join(dir, "item"))
					if err != nil {
						t.Fatal(err)
					}
					defer file.Close()

					_, err = file.Write(make([]byte, 1))
					if err != nil {
						t.Fatal(err)
					}

					return dir
				}(),
			},
			out: nil,
			err: nil,
		},
	}

	for name, tc := range testcases {
		tc := tc

		t.Run(name, func(t *testing.T) {
			t.Parallel()

			_, err := NewFSCache(tc.in.maxSize, tc.in.path)
			assert.Equal(t, tc.err, err)
		})
	}
}

func TestFSCacheAdd(t *testing.T) {
	type in struct {
		cache *FSCache
		items []*strings.Reader
	}

	testcases := map[string]struct {
		in in
		// used to check internal state of the cache
		// should return true if state is valid
		out func(cache *FSCache) bool
		err error
	}{
		"item that doesnt fit": {
			in: in{
				cache: func() *FSCache {
					cache, err := NewFSCache(1, t.TempDir())
					if err != nil {
						t.Fatal(err)
					}
					return cache
				}(),
				items: []*strings.Reader{
					strings.NewReader("value"),
				},
			},
			out: nil,
			err: ErrCacheSizeExceeded,
		},
		"item that fits": {
			in: in{
				cache: func() *FSCache {
					cache, err := NewFSCache(1, t.TempDir())
					if err != nil {
						t.Fatal(err)
					}
					return cache
				}(),
				items: []*strings.Reader{
					strings.NewReader("x"),
				},
			},
			out: nil,
			err: nil,
		},
		"index size is extended": {
			in: in{
				cache: func() *FSCache {
					cache, err := NewFSCache(2, t.TempDir(), WithIndexSize(1))
					if err != nil {
						t.Fatal(err)
					}
					return cache
				}(),
				items: []*strings.Reader{
					strings.NewReader("x"),
					strings.NewReader("x"),
				},
			},
			out: func(cache *FSCache) bool {
				return cache.indexSize == 2 && cache.index.Len() == 2
			},
			err: nil,
		},
		"when new item doesnt fit old item is evicted": {
			in: in{
				cache: func() *FSCache {
					cache, err := NewFSCache(1, t.TempDir(), WithIndexSize(1))
					if err != nil {
						t.Fatal(err)
					}
					return cache
				}(),
				items: []*strings.Reader{
					strings.NewReader("o"),
					strings.NewReader("x"),
				},
			},
			out: func(cache *FSCache) bool {
				return cache.indexSize == 1 && cache.index.Len() == 1
			},
			err: nil,
		},
		"on evict item is deleted from file system and index": {
			in: in{
				cache: func() *FSCache {
					cache, err := NewFSCache(1, t.TempDir(), WithIndexSize(1))
					if err != nil {
						t.Fatal(err)
					}
					return cache
				}(),
				items: []*strings.Reader{
					strings.NewReader("o"),
					strings.NewReader("x"),
				},
			},
			out: func(cache *FSCache) bool {
				_, ok := cache.index.Get("0")
				_, err := os.Stat(path.Join(cache.path, "0"))

				return !ok && errors.Is(err, os.ErrNotExist)
			},
			err: nil,
		},
		"add same key twice": {
			in: in{
				cache: func() *FSCache {
					cache, err := NewFSCache(2, t.TempDir(), WithIndexSize(2))
					if err != nil {
						t.Fatal(err)
					}
					cache.Add("0", strings.NewReader("x"), 1)
					return cache
				}(),
				items: []*strings.Reader{
					strings.NewReader("x"),
				},
			},
			out: nil,
			err: ErrKeyExist,
		},
	}

	for name, tc := range testcases {
		tc := tc

		t.Run(name, func(t *testing.T) {
			t.Parallel()

			for i, item := range tc.in.items {
				err := tc.in.cache.Add(strconv.Itoa(i), item, item.Size())
				assert.Equal(t, tc.err, err)
			}

			if tc.out != nil {
				assert.True(t, tc.out(tc.in.cache))
			}
		})
	}
}

func TestFSCacheGet(t *testing.T) {
	type in struct {
		cache *FSCache
		items map[string][]byte
	}

	testcases := map[string]struct {
		in  in
		out map[string][]byte
		err error
	}{
		"get item that doesnt exist": {
			in: in{
				cache: func() *FSCache {
					cache, err := NewFSCache(1, t.TempDir())
					if err != nil {
						t.Fatal(err)
					}
					return cache
				}(),
			},
			out: map[string][]byte{
				"x": []byte("x"),
			},
			err: ErrKeyDoesntExist,
		},
		"get existing item": {
			in: in{
				cache: func() *FSCache {
					cache, err := NewFSCache(5, t.TempDir())
					if err != nil {
						t.Fatal(err)
					}
					return cache
				}(),
				items: map[string][]byte{
					"x": []byte("value"),
				},
			},
			out: map[string][]byte{
				"x": []byte("value"),
			},
			err: nil,
		},
	}

	for name, tc := range testcases {
		tc := tc

		t.Run(name, func(t *testing.T) {
			t.Parallel()

			for k, v := range tc.in.items {
				err := tc.in.cache.Add(k, bytes.NewReader(v), int64(len(v)))
				assert.NoError(t, err)
			}

			for k, v := range tc.out {
				data, err := tc.in.cache.Get(k)
				assert.Equal(t, tc.err, err)

				if tc.err == nil {
					assert.NotNil(t, data)
					result := make([]byte, len(v))
					_, err := data.Read(result)
					assert.NoError(t, err)
					assert.Equal(t, v, result)
				}
			}
		})
	}
}
