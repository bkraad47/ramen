---
name: get_calculation_prompt
description: Solve an arithmetic request by calling demo_calculator_tool one operation at a time.
---
# Calculation

Request: {{request}}

1. Break the request into binary operations (add, subtract, multiply, divide).
2. Call `demo_calculator_tool` for each step, passing the previous result forward.
3. Reply with the final number and the steps taken. Do not compute in your head.
