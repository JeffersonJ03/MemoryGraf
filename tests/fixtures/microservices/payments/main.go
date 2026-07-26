package main

import "fmt"

// PaymentsRoute es el endpoint que expone este servicio.
const PaymentsRoute = "/api/payments"

func validate(amount int) bool {
	return amount > 0
}

// Charge cobra un importe si es válido.
func Charge(amount int) string {
	if validate(amount) {
		return fmt.Sprintf("charged %d via %s", amount, PaymentsRoute)
	}
	return "declined"
}
