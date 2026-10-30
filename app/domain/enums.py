"""Domain enums for the disputes & claims decision engine."""
from enum import Enum


class PaymentChannel(str, Enum):
    """Wires are out of scope (UCC Article 4A, not Reg E/Z) — see PLAN.md §15.14."""
    CARD_CREDIT = "CARD_CREDIT"
    CARD_DEBIT = "CARD_DEBIT"
    ZELLE = "ZELLE"
    ACH = "ACH"


class ClaimReason(str, Enum):
    FRAUD_UNAUTHORIZED = "FRAUD_UNAUTHORIZED"
    GOODS_NOT_RECEIVED = "GOODS_NOT_RECEIVED"
    NOT_AS_DESCRIBED = "NOT_AS_DESCRIBED"
    DUPLICATE_CHARGE = "DUPLICATE_CHARGE"
    CREDIT_NOT_PROCESSED = "CREDIT_NOT_PROCESSED"
    CANCELLED_RECURRING = "CANCELLED_RECURRING"
    INCORRECT_AMOUNT = "INCORRECT_AMOUNT"


class RegRegime(str, Enum):
    """Which regulatory clock set governs the case. Total routing table:
    CARD_DEBIT/ZELLE/ACH -> REG_E; CARD_CREDIT -> REG_Z."""
    REG_E = "REG_E"
    REG_Z = "REG_Z"


class NetworkCategory(str, Enum):
    """Card-network dispute category (VCR-shaped). CE 3.0 applies to FRAUD_CNP only.
    Zelle/ACH claims carry no network category (no chargeback rights)."""
    FRAUD_CNP = "FRAUD_CNP"                    # VCR 10.4, allocation
    FRAUD_CP = "FRAUD_CP"                      # VCR 10.x card-present
    CONSUMER_DISPUTE = "CONSUMER_DISPUTE"      # VCR 13.x, collaboration
    PROCESSING_ERROR = "PROCESSING_ERROR"      # VCR 12.x, collaboration


class CaseStatus(str, Enum):
    INTAKE = "INTAKE"
    TRIAGE = "TRIAGE"
    INVESTIGATION = "INVESTIGATION"
    PENDING_REVIEW = "PENDING_REVIEW"          # human checkpoint
    REPRESENTMENT = "REPRESENTMENT"            # network-facing recovery in flight
    RESOLVED_PAID = "RESOLVED_PAID"            # consumer favor, loss taken or recovered
    RESOLVED_DENIED = "RESOLVED_DENIED"        # human-approved denial only
    CLOSED = "CLOSED"


class Tier(str, Enum):
    """Arbitration-ladder tiers: discrete, reviewable policy objects."""
    T0_AUTO_PAY = "T0_AUTO_PAY"
    T1_PROVISIONAL_FAST = "T1_PROVISIONAL_FAST"
    T2_ENHANCED_INVESTIGATION = "T2_ENHANCED_INVESTIGATION"
    T3_DEFENDED_DENIAL_CANDIDATE = "T3_DEFENDED_DENIAL_CANDIDATE"


class ClockType(str, Enum):
    REG_E_INVESTIGATE = "REG_E_INVESTIGATE"            # 10 bd (20 bd new account)
    REG_E_PC_NOTICE = "REG_E_PC_NOTICE"                # 2 bd after provisional credit
    REG_E_RESOLVE = "REG_E_RESOLVE"                    # 45 cd (90 cd POS/foreign/new)
    REG_E_CORRECT = "REG_E_CORRECT"                    # 1 bd after determination
    REG_E_REPORT = "REG_E_REPORT"                      # 3 bd after determination
    REG_Z_ACK = "REG_Z_ACK"                            # 30 cd acknowledgment
    REG_Z_RESOLVE = "REG_Z_RESOLVE"                    # min(2 billing-cycle closes, 90 cd)
    VCR_MERCHANT_RESPONSE = "VCR_MERCHANT_RESPONSE"    # 30 cd representment window
    NETWORK_FILING_WINDOW = "NETWORK_FILING_WINDOW"    # ~120 cd from txn to file chargeback
    INTERNAL_SLA = "INTERNAL_SLA"                      # ops-defined internal target


class ClockState(str, Enum):
    RUNNING = "RUNNING"
    AT_RISK = "AT_RISK"
    BREACHED = "BREACHED"
    SATISFIED = "SATISFIED"
    CANCELLED = "CANCELLED"


class DecisionAction(str, Enum):
    AUTO_PAY = "AUTO_PAY"                              # final consumer-favor resolution
    POST_PROVISIONAL_CREDIT = "POST_PROVISIONAL_CREDIT"
    ROUTE_STANDARD_INVESTIGATION = "ROUTE_STANDARD_INVESTIGATION"
    ROUTE_ENHANCED_INVESTIGATION = "ROUTE_ENHANCED_INVESTIGATION"
    ROUTE_HUMAN_REVIEW = "ROUTE_HUMAN_REVIEW"
    FILE_CHARGEBACK = "FILE_CHARGEBACK"
    APPROVE_DENIAL = "APPROVE_DENIAL"                  # human-only
    APPROVE_PAY = "APPROVE_PAY"                        # human-approved consumer favor
    REVERSE_PROVISIONAL_CREDIT = "REVERSE_PROVISIONAL_CREDIT"  # human-only, notice+grace
    ACTIVATE_REG_Z_PENDENCY = "ACTIVATE_REG_Z_PENDENCY"  # payment hold, FC freeze, no CRA


class Actor(str, Enum):
    SYSTEM = "SYSTEM"
    HUMAN = "HUMAN"


class TrueType(str, Enum):
    """Ground-truth label used by the simulator (never visible to the engine at decision
    time; revealed only as investigation/outcome evidence)."""
    THIRD_PARTY_FRAUD = "THIRD_PARTY_FRAUD"
    MERCHANT_ERROR = "MERCHANT_ERROR"
    FIRST_PARTY_MISUSE = "FIRST_PARTY_MISUSE"
    LEGITIMATE_OTHER = "LEGITIMATE_OTHER"      # e.g. genuine non-fraud billing error


class Cohort(str, Enum):
    CHAMPION = "CHAMPION"
    CHALLENGER = "CHALLENGER"
