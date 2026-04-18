"""Extract validation guards, const declarations, joi/zod schemas, env-var lookups,
Temporal retry policies, and gRPC status mappings from JS/TS source.
"""

from __future__ import annotations

import re


def extract_code_facts(content: str, file_path: str, repo_name: str) -> list[dict]:
    """Extract validation guards, const declarations, and joi/zod schemas from JS code.

    Returns list of dicts with: repo_name, file_path, function_name, fact_type,
    condition, message, line_number, raw_snippet.
    """
    facts: list[dict] = []
    lines = content.splitlines()

    # Track current function scope
    current_function = None

    for i, line in enumerate(lines):
        stripped = line.strip()

        # Track function scope
        func_match = re.match(
            r"(?:async\s+)?function\s+(\w+)|(?:const|let|var)\s+(\w+)\s*=\s*(?:async\s+)?\(?|(\w+)\s*[:(]\s*(?:async\s+)?(?:function|\()",
            stripped,
        )
        if func_match:
            current_function = func_match.group(1) or func_match.group(2) or func_match.group(3)

        # Pattern 1: Validation guards — if (condition) throw/return error
        if_throw = re.match(
            r"if\s*\((.+?)\)\s*\{?\s*$",
            stripped,
        )
        if if_throw:
            condition = if_throw.group(1)
            # Look ahead for throw/return/error within next 10 lines
            for j in range(i + 1, min(i + 11, len(lines))):
                next_line = lines[j].strip()
                # Match throw, issuer_response_text, message patterns
                throw_match = re.search(r"throw\s+(?:new\s+\w+\()?['\"]([^'\"]+)['\"]", next_line) or re.search(
                    r"throw\s+(?:new\s+\w+\()?`([^`]+)`", next_line
                )
                response_match = re.search(r"issuer_response_text:\s*['\"]([^'\"]+)['\"]", next_line)
                message_match = re.search(r"message:\s*['\"]([^'\"]+)['\"]", next_line)
                msg = None
                if throw_match:
                    msg = throw_match.group(1)
                elif response_match:
                    msg = response_match.group(1)
                elif message_match:
                    msg = message_match.group(1)

                if msg:
                    snippet = "\n".join(lines[max(0, i - 1) : min(len(lines), j + 2)])
                    facts.append(
                        {
                            "repo_name": repo_name,
                            "file_path": file_path,
                            "function_name": current_function,
                            "fact_type": "validation_guard",
                            "condition": condition,
                            "message": msg,
                            "line_number": i + 1,
                            "raw_snippet": snippet[:500],
                        }
                    )
                    break

        # Pattern 2: Const declarations with literal values
        const_match = re.match(
            r"(?:const|let|var)\s+([A-Z][A-Z_0-9]+)\s*=\s*(.+?)(?:;?\s*$)",
            stripped,
        )
        if const_match:
            name = const_match.group(1)
            value = const_match.group(2).strip().rstrip(";")
            # Only index simple values (numbers, strings, small arrays)
            if len(value) < 300 and not value.startswith("require") and not value.startswith("function"):
                facts.append(
                    {
                        "repo_name": repo_name,
                        "file_path": file_path,
                        "function_name": current_function,
                        "fact_type": "const_value",
                        "condition": name,
                        "message": value,
                        "line_number": i + 1,
                        "raw_snippet": stripped[:500],
                    }
                )

        # Pattern 3: Joi schemas
        joi_match = re.search(r"Joi\.(object|string|number|array|boolean)\s*\(", stripped)
        if joi_match and ("validate" in stripped.lower() or "schema" in stripped.lower() or "=" in stripped):
            facts.append(
                {
                    "repo_name": repo_name,
                    "file_path": file_path,
                    "function_name": current_function,
                    "fact_type": "joi_schema",
                    "condition": stripped[:200],
                    "message": "",
                    "line_number": i + 1,
                    "raw_snippet": "\n".join(lines[max(0, i) : min(len(lines), i + 5)])[:500],
                }
            )

        # Pattern 4: process.env lookups with defaults
        env_match = re.search(
            r"process\.env\.(\w+)\s*(?:\|\||===?\s*|!==?\s*|\?\?)\s*['\"`]?([^'\"`;\n,)]{1,100})['\"`]?",
            stripped,
        )
        if env_match:
            env_name = env_match.group(1)
            default_val = env_match.group(2).strip()
            facts.append(
                {
                    "repo_name": repo_name,
                    "file_path": file_path,
                    "function_name": current_function,
                    "fact_type": "env_var",
                    "condition": env_name,
                    "message": default_val,
                    "line_number": i + 1,
                    "raw_snippet": stripped[:500],
                }
            )

        # Pattern 5: Temporal activity retry policies
        if "maximumAttempts" in stripped or "backoffCoefficient" in stripped or "initialInterval" in stripped:
            retry_match = re.search(
                r"(maximumAttempts|backoffCoefficient|initialInterval|startToCloseTimeout)"
                r"\s*:\s*['\"]?([^'\",}\s]+)['\"]?",
                stripped,
            )
            if retry_match:
                facts.append(
                    {
                        "repo_name": repo_name,
                        "file_path": file_path,
                        "function_name": current_function,
                        "fact_type": "temporal_retry",
                        "condition": retry_match.group(1),
                        "message": retry_match.group(2),
                        "line_number": i + 1,
                        "raw_snippet": "\n".join(lines[max(0, i - 2) : min(len(lines), i + 3)])[:500],
                    }
                )

        # Pattern 6: gRPC status code mapping
        grpc_status_match = re.search(
            r"(?:code|status)\s*:\s*(?:grpc\.status\.|status\.)?(\w+)\s*,\s*(?:message|details)\s*:\s*['\"`]([^'\"`]+)['\"`]",
            stripped,
        )
        if grpc_status_match:
            facts.append(
                {
                    "repo_name": repo_name,
                    "file_path": file_path,
                    "function_name": current_function,
                    "fact_type": "grpc_status",
                    "condition": grpc_status_match.group(1),
                    "message": grpc_status_match.group(2),
                    "line_number": i + 1,
                    "raw_snippet": stripped[:500],
                }
            )

    return facts
