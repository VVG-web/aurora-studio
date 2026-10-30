package main

import "fmt"

// Warehouse stock check: the only hand-written code in this repository.
func main() {
	fmt.Println(stockLeft(10, 3))
}

func stockLeft(total, taken int) int {
	return total - taken
}
