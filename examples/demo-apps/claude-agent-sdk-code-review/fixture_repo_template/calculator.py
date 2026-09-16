"""A tiny calculator module -- deliberately ships with one bug for the demo."""


def add(a, b):
    return a + b


def subtract(a, b):
    return a - b


def average(numbers):
    # Bug: integer division truncates in some callers' expectations,
    # and doesn't guard against an empty list.
    return sum(numbers) / len(numbers)


def percentage_change(old, new):
    # Bug: divides by `new` instead of `old`.
    return (new - old) / new * 100
