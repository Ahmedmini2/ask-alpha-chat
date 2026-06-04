"""Deterministic real-estate financial calculators for the UAE market.

These tools do pure arithmetic on numbers the user/LLM supplies — they don't read
the DB. Rules (LTV caps, DLD fee, Golden Visa threshold) reflect standard UAE
practice and are labeled as assumptions so the model presents them transparently.
"""
from typing import Any
from app.tools.registry import Tool, registry

# Standard UAE costs / thresholds (assumptions — surface them to the user).
DLD_TRANSFER_PCT = 4.0          # Dubai Land Department transfer fee
AGENT_COMMISSION_PCT = 2.0      # typical buyer agent commission
MORTGAGE_REG_PCT = 0.25         # DLD mortgage registration (of loan)
GOLDEN_VISA_THRESHOLD_AED = 2_000_000


def _num(v: Any) -> float:
    return float(v)


# ---------------------------------------------------------------- mortgage
async def calculate_mortgage_handler(db, args: dict, ctx: dict) -> dict:
    try:
        price = _num(args["price"])
    except (KeyError, TypeError, ValueError):
        return {"error": "price is required"}
    annual_rate = float(args.get("annual_rate_pct", 4.5))
    years = int(args.get("years", 25))
    is_resident = bool(args.get("is_resident", True))
    is_first_home = bool(args.get("is_first_home", True))

    # UAE LTV caps (max % of price that can be financed).
    if is_resident:
        max_ltv = 80.0 if (price < 5_000_000 and is_first_home) else (70.0 if is_first_home else 65.0)
    else:
        max_ltv = 60.0 if price < 5_000_000 else 50.0
    min_down_pct = round(100.0 - max_ltv, 1)

    down_pct = args.get("down_payment_pct")
    if down_pct is None and args.get("down_payment") is not None:
        down_pct = _num(args["down_payment"]) / price * 100.0
    if down_pct is None:
        down_pct = min_down_pct
    down_pct = float(down_pct)

    below_minimum = down_pct < min_down_pct - 1e-9
    effective_down_pct = max(down_pct, min_down_pct)
    down_payment = round(price * effective_down_pct / 100.0, 0)
    loan = round(price - down_payment, 0)

    r = annual_rate / 100.0 / 12.0
    n = years * 12
    if r > 0:
        monthly = loan * r / (1 - (1 + r) ** (-n))
    else:
        monthly = loan / n
    total_paid = monthly * n
    total_interest = total_paid - loan

    return {
        "price_aed": price,
        "max_ltv_pct": max_ltv,
        "min_down_payment_pct": min_down_pct,
        "down_payment_pct_used": round(effective_down_pct, 1),
        "down_payment_aed": down_payment,
        "loan_amount_aed": loan,
        "annual_rate_pct": annual_rate,
        "term_years": years,
        "monthly_payment_aed": round(monthly, 0),
        "total_interest_aed": round(total_interest, 0),
        "total_repayment_aed": round(total_paid, 0),
        "note": (
            f"Down payment raised to the {min_down_pct:.0f}% UAE minimum for this case."
            if below_minimum else
            f"UAE max LTV for this case is {max_ltv:.0f}% (min {min_down_pct:.0f}% down)."
        ),
        "assumptions": "UAE LTV caps for residents/non-residents; rate and term are inputs, not advice.",
    }


# ---------------------------------------------------------------- yield
async def calculate_rental_yield_handler(db, args: dict, ctx: dict) -> dict:
    try:
        price = _num(args["price"])
        annual_rent = _num(args["annual_rent"])
    except (KeyError, TypeError, ValueError):
        return {"error": "price and annual_rent are required"}
    service_charge = float(args.get("annual_service_charge", 0) or 0)
    other_costs = float(args.get("annual_other_costs", 0) or 0)

    gross = annual_rent / price * 100.0 if price else None
    net_income = annual_rent - service_charge - other_costs
    net = net_income / price * 100.0 if price else None
    return {
        "price_aed": price,
        "annual_rent_aed": annual_rent,
        "annual_service_charge_aed": service_charge,
        "annual_other_costs_aed": other_costs,
        "gross_yield_pct": round(gross, 2) if gross is not None else None,
        "net_yield_pct": round(net, 2) if net is not None else None,
        "net_annual_income_aed": round(net_income, 0),
        "note": "Gross = rent/price. Net deducts service charge and other costs.",
    }


# ---------------------------------------------------------------- payment plan
async def payment_plan_breakdown_handler(db, args: dict, ctx: dict) -> dict:
    try:
        price = _num(args["price"])
    except (KeyError, TypeError, ValueError):
        return {"error": "price is required"}
    during_pct = float(args.get("during_construction_pct", 60))
    handover_pct = float(args.get("on_handover_pct", 40))
    post_pct = float(args.get("post_handover_pct", 0))
    post_years = int(args.get("post_handover_years", 0))

    total_pct = during_pct + handover_pct + post_pct
    schedule = [
        {"milestone": "During construction", "pct": during_pct, "amount_aed": round(price * during_pct / 100, 0)},
        {"milestone": "On handover", "pct": handover_pct, "amount_aed": round(price * handover_pct / 100, 0)},
    ]
    if post_pct > 0:
        per_year = round(price * post_pct / 100 / max(post_years, 1), 0)
        schedule.append({
            "milestone": f"Post-handover over {post_years}y", "pct": post_pct,
            "amount_aed": round(price * post_pct / 100, 0), "approx_per_year_aed": per_year,
        })
    return {
        "price_aed": price,
        "schedule": schedule,
        "total_pct": total_pct,
        "valid": abs(total_pct - 100.0) < 0.01,
        "note": ("Percentages don't sum to 100%; check the plan." if abs(total_pct - 100.0) >= 0.01
                 else "Standard milestone split."),
    }


# ---------------------------------------------------------------- total cost
async def total_cost_of_ownership_handler(db, args: dict, ctx: dict) -> dict:
    try:
        price = _num(args["price"])
    except (KeyError, TypeError, ValueError):
        return {"error": "price is required"}
    mortgaged = bool(args.get("mortgaged", False))
    loan_amount = float(args.get("loan_amount", 0) or 0)

    dld = price * DLD_TRANSFER_PCT / 100.0
    commission = price * AGENT_COMMISSION_PCT / 100.0
    dld_admin = 4200.0  # standard property registration admin fee
    mortgage_reg = (loan_amount * MORTGAGE_REG_PCT / 100.0 + 290.0) if mortgaged and loan_amount else 0.0
    upfront_fees = dld + commission + dld_admin + mortgage_reg
    return {
        "price_aed": price,
        "fees": {
            "dld_transfer_4pct_aed": round(dld, 0),
            "agent_commission_2pct_aed": round(commission, 0),
            "dld_admin_aed": dld_admin,
            "mortgage_registration_aed": round(mortgage_reg, 0),
        },
        "total_upfront_fees_aed": round(upfront_fees, 0),
        "all_in_cost_aed": round(price + upfront_fees, 0),
        "fees_as_pct_of_price": round(upfront_fees / price * 100, 2),
        "assumptions": f"DLD {DLD_TRANSFER_PCT}%, commission {AGENT_COMMISSION_PCT}%, admin AED 4,200"
                       + (", mortgage reg 0.25% of loan + AED 290" if mortgaged else ""),
    }


# ---------------------------------------------------------------- golden visa
async def check_golden_visa_handler(db, args: dict, ctx: dict) -> dict:
    try:
        value = _num(args["property_value"])
    except (KeyError, TypeError, ValueError):
        return {"error": "property_value is required"}
    eligible = value >= GOLDEN_VISA_THRESHOLD_AED
    shortfall = max(0.0, GOLDEN_VISA_THRESHOLD_AED - value)
    return {
        "property_value_aed": value,
        "threshold_aed": GOLDEN_VISA_THRESHOLD_AED,
        "eligible": eligible,
        "shortfall_aed": round(shortfall, 0) if not eligible else 0,
        "explanation": (
            "At or above AED 2,000,000, this property can qualify the buyer for a 10-year "
            "UAE Golden Visa (real-estate route). Off-plan from approved developers and "
            "mortgaged properties can also qualify subject to conditions."
            if eligible else
            f"Below the AED 2,000,000 Golden Visa property threshold by AED {shortfall:,.0f}."
        ),
        "assumptions": "Golden Visa real-estate threshold is AED 2M; final eligibility is set by ICP/GDRFA.",
    }


# ---------------------------------------------------------------- registrations
registry.register(Tool(
    name="calculate_mortgage",
    description=(
        "Calculate a UAE mortgage: applies the correct loan-to-value cap (resident vs non-resident, "
        "first home vs not, above/below AED 5M), then returns the down payment, loan amount, monthly "
        "payment, and total interest. Use when the user asks about mortgage, financing, monthly "
        "payments, or how much deposit they need. Provide price; rate/term/down payment optional."
    ),
    input_schema={
        "type": "object",
        "properties": {
            "price": {"type": "number", "description": "Property price in AED."},
            "down_payment_pct": {"type": "number", "description": "Down payment as a % of price. If omitted, the legal minimum is used."},
            "down_payment": {"type": "number", "description": "Down payment as an absolute AED amount (alternative to down_payment_pct)."},
            "annual_rate_pct": {"type": "number", "description": "Annual interest rate %, default 4.5.", "default": 4.5},
            "years": {"type": "integer", "description": "Loan term in years, default 25.", "default": 25},
            "is_resident": {"type": "boolean", "description": "UAE resident buyer? Affects LTV cap. Default true.", "default": True},
            "is_first_home": {"type": "boolean", "description": "First property purchase? Affects LTV cap. Default true.", "default": True},
        },
        "required": ["price"],
    },
    handler=calculate_mortgage_handler,
))

registry.register(Tool(
    name="calculate_rental_yield",
    description=(
        "Calculate gross and net rental yield from a property price and annual rent. Net deducts the "
        "annual service charge and any other costs. Use when the user gives a price and a rent and asks "
        "about yield, return, or net income."
    ),
    input_schema={
        "type": "object",
        "properties": {
            "price": {"type": "number", "description": "Property price in AED."},
            "annual_rent": {"type": "number", "description": "Expected annual rent in AED."},
            "annual_service_charge": {"type": "number", "description": "Annual service charge in AED (optional)."},
            "annual_other_costs": {"type": "number", "description": "Other annual costs in AED (optional)."},
        },
        "required": ["price", "annual_rent"],
    },
    handler=calculate_rental_yield_handler,
))

registry.register(Tool(
    name="payment_plan_breakdown",
    description=(
        "Break a developer payment plan into cash amounts. Give the price and the split (during "
        "construction %, on handover %, optional post-handover % over N years). Returns the AED amount "
        "at each milestone. Use for 'what would a 60/40 plan cost me' style questions."
    ),
    input_schema={
        "type": "object",
        "properties": {
            "price": {"type": "number", "description": "Property price in AED."},
            "during_construction_pct": {"type": "number", "description": "% paid during construction (default 60).", "default": 60},
            "on_handover_pct": {"type": "number", "description": "% paid on handover (default 40).", "default": 40},
            "post_handover_pct": {"type": "number", "description": "% paid after handover (default 0).", "default": 0},
            "post_handover_years": {"type": "integer", "description": "Years to spread the post-handover portion over.", "default": 0},
        },
        "required": ["price"],
    },
    handler=payment_plan_breakdown_handler,
))

registry.register(Tool(
    name="total_cost_of_ownership",
    description=(
        "Compute the all-in purchase cost of a Dubai property: price + DLD transfer fee (4%) + agent "
        "commission (2%) + admin + (if mortgaged) mortgage registration. Use when the user asks the "
        "true/total cost, closing costs, or fees on top of the price."
    ),
    input_schema={
        "type": "object",
        "properties": {
            "price": {"type": "number", "description": "Property price in AED."},
            "mortgaged": {"type": "boolean", "description": "Is the purchase mortgaged? Adds mortgage registration. Default false.", "default": False},
            "loan_amount": {"type": "number", "description": "Loan amount in AED (used for mortgage registration if mortgaged)."},
        },
        "required": ["price"],
    },
    handler=total_cost_of_ownership_handler,
))

registry.register(Tool(
    name="check_golden_visa",
    description=(
        "Check whether a property value qualifies for the UAE 10-year Golden Visa (real-estate route, "
        "AED 2,000,000 threshold) and explain. Use when the user asks about residency/Golden Visa "
        "eligibility via property."
    ),
    input_schema={
        "type": "object",
        "properties": {
            "property_value": {"type": "number", "description": "Property value in AED."},
        },
        "required": ["property_value"],
    },
    handler=check_golden_visa_handler,
))
