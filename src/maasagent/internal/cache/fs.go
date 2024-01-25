//go:build linux

package cache

import (
	"errors"
	"fmt"
	"io"
	"io/fs"
	"os"
	"path"
	"sync"
	"syscall"

	lru "github.com/hashicorp/golang-lru/v2"
)

const (
	Kilobyte = 1024
	Megabyte = 1024 * Kilobyte
	Gigabyte = 1024 * Megabyte
)

const (
	// this is just a semi-random starting number
	defaultIndexSize = 50
)

var (
	ErrKeyExist             = errors.New("key already exist")
	ErrFileDoesntExist      = errors.New("file doesn't exist")
	ErrCacheSizeExceeded    = errors.New("cache size exceeded")
	ErrMissingCachePath     = errors.New("missing cache path")
	ErrPositiveMaxCacheSize = errors.New("cache size must be positive")
	ErrKeyDoesntExist       = errors.New("key doesn't exist")
)

// FSCache implements a file system backed cache, which makes it possible
// to store values as files on disk. Total cache size is specified by the
// total size of the data, rather than the amount of items.
// FSCache maintains index of all the items in a in-memory LRU cache, that
// allows to free up space by evicting oldest items.
type FSCache struct {
	// index keeps information about added keys
	index *lru.Cache[string, string]
	// required to track if the size of LRU cache (index) should be increased.
	// This is expected to happen because we limit our cache based on the
	// disk space size and not on the items size.
	indexSize int
	// path used to store cached items
	path string
	// max cache size
	maxSize int64
	// current cache size
	size int64
	sync.RWMutex
}

// NewFSCache instantiates a new instance of FSCache.
// If the provided path does not exist, it will be created,
// otherwise existing files will be indexed and served from cache.
func NewFSCache(maxSize int64, path string, options ...FSCacheOption) (*FSCache, error) {
	if maxSize <= 0 {
		return nil, ErrPositiveMaxCacheSize
	}

	if path == "" {
		return nil, ErrMissingCachePath
	}

	cache := &FSCache{
		indexSize: defaultIndexSize,
		path:      path,
		maxSize:   maxSize,
	}

	for _, opt := range options {
		opt(cache)
	}

	index, err := lru.New[string, string](cache.indexSize)
	if err != nil {
		return nil, err
	}

	cache.index = index

	if _, err := os.Stat(path); err != nil {
		if errors.Is(err, fs.ErrNotExist) && os.MkdirAll(path, 0750) == nil {
			return cache, nil
		}

		return nil, err
	}

	// Because cache directory is not empty, we need to build index
	// of existing files.
	return cache, cache.reindex()
}

// FSCacheOption allows to set additional FSCache options
type FSCacheOption func(*FSCache)

// WithIndexSize allows to set maximum index size.
// Normally this is required only in tests.
func WithIndexSize(i int) FSCacheOption {
	return func(c *FSCache) {
		c.indexSize = i
	}
}

func (c *FSCache) Add(key string, value io.Reader, valueSize int64) error {
	return c.add(key, value, valueSize)
}

func (c *FSCache) Get(key string) (io.ReadCloser, error) {
	return c.get(key)
}

func (c *FSCache) add(key string, value io.Reader, valueSize int64) (err error) {
	// Because of the cleanup logic that happens in defer func() we have to use
	// named return variable here, so we can return error happened during cleanup.
	c.Lock()
	defer c.Unlock()

	_, ok := c.index.Get(key)
	if ok {
		return ErrKeyExist
	}

	if valueSize > c.maxSize && c.size+valueSize > c.maxSize {
		return ErrCacheSizeExceeded
	}

	filePath := path.Join(c.path, key)
	// Remove oldest files in order to fit new item into cache.
	for c.size+valueSize > c.maxSize {
		err = c.evict()
		if err != nil {
			return fmt.Errorf("failed to evict oldest value: %w", err)
		}
	}

	var f *os.File
	//nolint:gosec // gosec wants string literal file arguments only
	f, err = os.OpenFile(filePath, os.O_CREATE|os.O_EXCL|os.O_RDWR, 0600)
	if err != nil {
		return fmt.Errorf("failed opening file: %w", err)
	}

	defer func() {
		if err != nil {
			closeErr := f.Close()
			if closeErr != nil {
				err = errors.Join(err, closeErr)
			}
			removeErr := os.Remove(filePath)
			if removeErr != nil && !errors.Is(err, fs.ErrNotExist) {
				err = errors.Join(err, removeErr)
			}
		}
	}()

	if err = c.preallocate(f, valueSize); err != nil {
		return fmt.Errorf("failed to allocate space for new file: %w", err)
	}

	c.size += valueSize

	_, err = io.Copy(f, value)
	if err != nil {
		return fmt.Errorf("failed to write value: %w", err)
	}

	if err = f.Sync(); err != nil {
		return err
	}

	// Extend the size of LRU cache if we hit maxSize.
	// This is expected to happen because we limit our cache based on the
	// disk space size.
	if c.index.Len()+1 > c.indexSize {
		c.indexSize += c.indexSize
		c.index.Resize(c.indexSize)
	}

	// Return value is not checked, because index always grows.
	c.index.Add(key, filePath)

	return nil
}

func (c *FSCache) get(key string) (io.ReadCloser, error) {
	c.RLock()
	defer c.RUnlock()

	v, ok := c.index.Get(key)
	if !ok {
		return nil, ErrKeyDoesntExist
	}

	//nolint:gosec // gosec wants string literal file arguments
	return os.OpenFile(v, os.O_RDONLY, 0600)
}

// preallocate method is used to check if there is enough disk space
// to store the item and fail early if the file doesn't fit.
func (c *FSCache) preallocate(f *os.File, size int64) error {
	fd := f.Fd()
	if fd <= 0 {
		return fmt.Errorf("invalid file descriptor: %w", os.ErrNotExist)
	}

	return syscall.Fallocate(int(fd), 0, 0, size)
}

// evict removes the oldest item from cache.
// Should be used only during add operation if new item doesn't fit.
func (c *FSCache) evict() error {
	key, idx, _ := c.index.GetOldest()
	stat, err := os.Stat(idx)
	if err != nil {
		return err
	}

	if err := os.Remove(idx); err != nil {
		return err
	}

	c.size -= stat.Size()
	c.index.Remove(key)

	return nil
}

// reindex walks the configured cache directory, creating
// index for files currently present in cache directory.
// If the size of existing files is bigger than cache maxSize,
// ErrCacheSizeExceeded is returned.
// Normally should be used only within NewFSCache function call
func (c *FSCache) reindex() error {
	dir := os.DirFS(c.path)

	return fs.WalkDir(dir, ".", func(fpath string, entry fs.DirEntry, err error) error {
		if err != nil {
			return err
		}

		if fpath == "." {
			return nil
		}

		fpath = path.Join(c.path, fpath)

		stat, err := entry.Info()
		if err != nil {
			return err
		}

		c.size += stat.Size()
		if c.size > c.maxSize {
			return ErrCacheSizeExceeded
		}

		c.index.Add(stat.Name(), fpath)

		return nil
	})
}
