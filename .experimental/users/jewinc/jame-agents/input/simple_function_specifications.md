<focused specification>
Purpose and behaviour:
- Computes the nth number in the Fibonacci sequence (0-indexed)
- Follows standard mathematical definition: fib(0)=0, fib(1)=1, fib(n)=fib(n-1)+fib(n-2)
- Optimized for time complexity O(n) using iterative approach

Inputs:
- Single integer n (non-negative, 0 ≤ n ≤ 1e4 typical implementation limit)

Outputs:
- Integer representing Fibonacci number at position n
- Returns -1 for invalid inputs (negative numbers, non-integer inputs)

Edge cases:
- n=0 → returns 0
- n=1 → returns 1
- Negative input → returns -1
- Non-integer input → returns -1
- Large n values (e.g., n=100 → 354224848179261915075)

Technical constraints:
- Requires O(1) space complexity
- Avoids recursion to prevent stack overflow
- Handles up to n=1e4 in under 1s (typical implementation)

Security considerations:
- Input validation to prevent injection attacks if used in web contexts
- Type checking to avoid code execution vulnerabilities
- Size limitation to prevent resource exhaustion attacks
</focused specification>