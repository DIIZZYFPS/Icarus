import math

def calculate_sqrt(n):
    if n < 0:
        return "Cannot calculate square root of a negative number"
    return math.sqrt(n)

if __name__ == "__main__":
    test_values = [16, 25, 100, 2]
    for val in test_values:
        print(f"The square root of {val} is {calculate_sqrt(val)}")
