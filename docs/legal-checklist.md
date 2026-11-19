# Legal pages: what is written and what the operator must still do

`clipcrow/service/static/legal.js` holds the Terms of Service and the Privacy Policy (shown at `#/terms` and `#/privacy`, readable without signing in, linked from the footer and from the sign-in screen's consent line). They are a **draft written from what the product does**; they are not legal advice.

Before a public launch, the operator must:
1. **Have a lawyer review both pages** (India first: the IT Act and the Digital Personal Data Protection Act 2023; GDPR if people in the EU or UK will sign up).
2. **Fill in who "we" are:** the legal entity name, registered address and a grievance officer (the DPDP Act and the IT Rules expect one), then put them in the text. Today the pages say "the operator of shackyapps.in".
3. **Decide the commercial terms that the draft leaves open:** the refund policy (the draft says unused minutes can be refunded on request within a reasonable time: make that exact, for example 7 days and only the unused part), the answer time for data requests (30 days is written), the limit of liability (three months of fees is written), the minimum age (18 is written) and the court's place.
4. **Keep the pages true.** If a third party is added (a new AI provider, a new GPU host, analytics, a new payment or mail service), the "Who else handles your data" section changes the same day. Retention (the plan's days), the faces-and-voices paragraph, the cookie statement and the "do not train models" line are claims about the code: re-check them when the code changes.
5. Consider a cookie banner only if non-essential cookies are ever added (today there is one strictly necessary session cookie).
6. Add a data-export route (a zip of a person's jobs) or promise a manual export by email; the policy promises a copy within 30 days on request.
