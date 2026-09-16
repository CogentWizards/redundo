from calculator import add, subtract, average, percentage_change


def test_add():
    assert add(2, 3) == 5


def test_subtract():
    assert subtract(5, 3) == 2


def test_average():
    assert average([1, 2, 3, 4]) == 2.5


def test_percentage_change():
    # Going from 50 to 75 is a 50% increase.
    assert percentage_change(50, 75) == 50.0
