package providers

import "time"

// nowMonotonic returns a monotonic clock reading in seconds, used for cooldown
// and block deadlines so wall-clock adjustments cannot extend them.
func nowMonotonic() float64 {
	return float64(time.Since(monotonicBase).Nanoseconds()) / 1e9
}

var monotonicBase = time.Now()
