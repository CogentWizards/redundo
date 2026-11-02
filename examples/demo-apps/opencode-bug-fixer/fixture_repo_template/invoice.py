"""Line-item totals for a small invoicing script."""


def line_total(quantity, unit_price, discount_pct=0):
    """Price for one line, after a percentage discount."""
    subtotal = quantity * unit_price
    return round(subtotal - subtotal * discount_pct, 2)


def invoice_total(lines):
    """Sum of every line's total. `lines` is a list of (quantity, unit_price, discount_pct)."""
    return round(sum(line_total(*line) for line in lines), 2)
