package server

import (
	"encoding/csv"
	"io"
)

// csvWriter buffers CSV rows and flushes them to the response writer.
type csvWriter struct {
	inner *csv.Writer
}

func newCSVWriter(w io.Writer) *csvWriter {
	return &csvWriter{inner: csv.NewWriter(w)}
}

func (c *csvWriter) write(row []string) error { return c.inner.Write(row) }

func (c *csvWriter) flush() { c.inner.Flush() }
