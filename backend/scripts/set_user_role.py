import argparse
import sys
from pathlib import Path

# Ensure the `backend/` root directory is on sys.path when invoked directly as `python scripts/set_user_role.py`
BACKEND_ROOT = Path(__file__).resolve().parents[1]
if str(BACKEND_ROOT) not in sys.path:
    sys.path.insert(0, str(BACKEND_ROOT))

from firebase_admin import auth  # noqa: E402
from app.core.security import initialize_firebase  # noqa: E402

VALID_ROLES = ("operator", "manager", "admin")


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Assign a Firebase custom role claim ('operator', 'manager', or 'admin') and/or displayName to a user by email."
    )
    parser.add_argument(
        "email",
        help="Email address of the Firebase user (e.g. user@example.com)",
    )
    parser.add_argument(
        "role",
        nargs="?",
        choices=VALID_ROLES,
        type=str.lower,
        default=None,
        help="Role to assign: operator, manager, or admin",
    )
    parser.add_argument(
        "-n",
        "--display-name",
        dest="display_name",
        default=None,
        help="Optional display name to set on the user's Firebase profile (e.g. 'Marco Ripa')",
    )
    parser.add_argument(
        "-p",
        "--pin",
        dest="pin",
        default=None,
        help="Optional 6-digit numeric PIN (e.g. '123456') to set as the user's login PIN",
    )
    parser.add_argument(
        "--ute-id",
        dest="ute_id",
        default=None,
        help="Optional Ute vehicle ID claim to associate with the user (e.g. ute-01)",
    )
    args = parser.parse_args()
    if (
        args.role is None
        and args.display_name is None
        and args.pin is None
        and args.ute_id is None
    ):
        parser.error(
            "At least one of 'role', '--display-name', '--pin', or '--ute-id' must be provided."
        )
    if args.pin is not None and (not args.pin.isdigit() or len(args.pin) != 6):
        parser.error("'--pin' must be exactly 6 numeric digits (0-9).")
    return args


def main() -> int:
    args = parse_args()

    if initialize_firebase() is None:
        print("Error: Failed to initialize Firebase Admin SDK.", file=sys.stderr)
        return 1

    try:
        user = auth.get_user_by_email(args.email)
    except auth.UserNotFoundError:
        print(f"Error: No Firebase user found with email '{args.email}'.", file=sys.stderr)
        return 1
    except Exception as exc:
        print(f"Error looking up user '{args.email}': {exc}", file=sys.stderr)
        return 1

    # Update displayName and/or 6-digit PIN on the Firebase Auth user record if requested
    update_kwargs = {}
    if args.display_name is not None:
        update_kwargs["display_name"] = args.display_name
    if args.pin is not None:
        update_kwargs["password"] = args.pin

    if update_kwargs:
        user = auth.update_user(user.uid, **update_kwargs)
        if args.display_name is not None:
            print(
                f"Successfully updated displayName for {user.email} (UID: {user.uid}) to: '{user.display_name}'"
            )
        if args.pin is not None:
            print(
                f"Successfully updated 6-digit PIN for {user.email} (UID: {user.uid})."
            )

    # Preserve any existing custom claims while updating role / ute_id
    if args.role is not None or args.ute_id is not None:
        claims = dict(user.custom_claims or {})
        if args.role is not None:
            claims["role"] = args.role
        if args.ute_id is not None:
            claims["ute_id"] = args.ute_id

        auth.set_custom_user_claims(user.uid, claims)
        print(
            f"Successfully updated {user.email} (UID: {user.uid}) with claims: {claims}"
        )

    return 0


if __name__ == "__main__":
    raise SystemExit(main())

