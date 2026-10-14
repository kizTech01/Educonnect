"""Initial content for the database-backed EduConnect policy publication."""

from datetime import date


POLICY_PUBLICATION_DATE = date(2026, 10, 10)
POLICY_VERSION = "1.0-draft"
LEGAL_REVIEW_NOTICE = (
    "This is a production draft for review by a qualified Nigerian lawyer and "
    "data-protection professional before final adoption. It is not legal advice "
    "and does not represent a signed institutional agreement."
)


def section(heading, *paragraphs):
    return {"heading": heading, "paragraphs": list(paragraphs)}


POLICY_CATALOG = {
    "privacy": {
        "title": "Privacy Policy",
        "summary": "How EduConnect handles personal and institutional information.",
        "acceptance_required": False,
        "sections": [
            section("Introduction and scope", "EduConnect is a university-management and learning platform operated by KizTech Technologies Ltd. This Privacy Policy explains the personal and institutional information processed when EduConnect is used by universities, their authorised staff, students, parents or guardians, applicants and other permitted users. Contact KizTech at kiztech01@gmail.com or 08128666531. We do not state a physical address, registration number or certification that has not been verified."),
            section("Information we process", "Depending on enabled features, EduConnect processes account and contact details; identity and profile information; institution, faculty, department, programme and course records; course registrations, results and attendance-related records; messages; payment references and status; uploaded documents; accommodation information; and technical and security information such as authentication, audit and request data. Institutions decide which records to place in the service."),
            section("Purpose, legal basis and responsibilities", "KizTech processes information to provide, secure, support and improve the contracted service; administer accounts; run authorised academic and administrative workflows; verify payment events; communicate about the service; and meet applicable legal obligations. Processing is undertaken in accordance with the Nigeria Data Protection Act 2023 where applicable. A university normally determines the purpose and means of processing its institutional records and is responsible for the notices, permissions and lawful basis it requires. KizTech normally processes that data as service provider or processor under the applicable agreement and documented instructions, except where law requires otherwise."),
            section("AI, payments and sharing", "Where enabled, EduConnect AI may process information supplied to the feature to generate assistance. AI output can be incomplete or inaccurate and must not be the sole basis for an academic, financial, disciplinary or other consequential decision. Payment functions may use Paystack or another configured provider; payment providers process their own transaction data under their terms. Information may be shared with authorised institutional users, service providers, professional advisers or authorities only where necessary, authorised or required. International transfer arrangements, if any, must be assessed under the relevant agreement and applicable law."),
            section("Retention, rights and children", "Retention depends on the university's instructions, applicable law, contract, security, backup and legitimate operational needs; this policy does not promise a universal retention period. A university may request return or deletion of institutional data subject to those limits. Individuals may request access, correction, deletion, restriction or objection through their university where it controls the record, or contact KizTech for platform-held information. Some students may be minors; institutions and authorised users are responsible for obtaining any notices, authority or consent required for their educational context."),
            section("Security, incidents and updates", "EduConnect uses access controls, tenant-aware application controls and other measures described in the Security & Data Protection Policy. No internet service can promise absolute security. KizTech will investigate, contain and remediate qualifying privacy or security incidents and notify affected institutions where appropriate and required by law or contract. Questions, complaints or requests may be sent to kiztech01@gmail.com or 08128666531. Material changes will be published as a new version with its publication information."),
        ],
    },
    "terms": {
        "title": "Terms and Conditions",
        "summary": "The rules for authorised use of EduConnect by institutions and users.",
        "acceptance_required": True,
        "sections": [
            section("Agreement and account access", "These Terms govern access to EduConnect. A user who registers, receives an account or uses the service must use it only within their authority and the enabled features of their institution. Institution administrators are responsible for onboarding and managing authorised users. Super Admins administer the platform; Institution Admins administer only their institution; lecturers, students and other role holders must stay within their assigned permissions."),
            section("User responsibilities", "Users must provide accurate information, keep credentials confidential, protect devices, promptly report suspected misuse and comply with institutional rules. Users may not share accounts, impersonate another person, access another tenant's data, alter academic or payment records without authority, or rely on an AI output without appropriate human verification. A parent or guardian may view only records explicitly made available to that account."),
            section("Institutional subscriptions and payment arrangements", "Institutional subscriptions, renewals and available payment methods are governed by the relevant order, plan and payment process. Departments and courses may have institution-configured fees. Payment status is verified through the applicable integration; a payment attempt alone is not proof of entitlement. Refund, cancellation and payment-dispute handling is described in the Refund & Cancellation Policy and may require the institution's participation."),
            section("Content, intellectual property and AI", "Users retain responsibility for content and records they lawfully upload. They grant the limited rights needed to host, process and display that content within the service. EduConnect software, branding and platform materials remain protected by applicable intellectual-property law. AI features are assistive only: users must review outputs, protect confidential information and not use AI to evade academic integrity, access controls or human review."),
            section("Suspension, termination and disputes", "KizTech or an authorised institution may restrict or suspend access to protect users, investigate misuse, meet legal obligations, enforce these Terms or address an expired or suspended subscription. Ending access does not automatically delete records where retention, backup, law or contract requires otherwise. These Terms are intended to be interpreted under applicable Nigerian law; specific dispute, notice and contractual arrangements should be set out in the relevant institutional agreement."),
        ],
    },
    "refunds": {
        "title": "Refund and Cancellation Policy",
        "summary": "How subscription and institution-controlled fee payment issues are handled.",
        "acceptance_required": False,
        "sections": [
            section("Scope and payment roles", "EduConnect may facilitate institutional subscription payments and, where an institution configures them, departmental and course-fee payments. KizTech does not set a university's departmental or course fee, academic entitlement or refund eligibility. Paystack or another configured provider may process a transaction and maintain transaction records under its own terms."),
            section("Institutional subscriptions", "Subscription payments are generally not refundable after a service period has begun, except where a duplicate, verified error, applicable law or the relevant institutional agreement requires a different outcome. Cancellation ordinarily stops a future renewal rather than reversing a completed period, unless the agreement or law provides otherwise. No unverified uptime, service-credit or cancellation promise is created by this policy."),
            section("Departmental and paid-course fees", "A request about a departmental levy, examination-related fee or paid course fee must ordinarily be made to the responsible university or department because it controls the fee and academic entitlement. KizTech may help verify a platform payment reference where appropriate, but does not override a lawful institutional decision or issue a refund from money controlled by the institution."),
            section("Duplicates, failures and disputes", "A duplicate or erroneous transaction should be reported promptly with the payer name, date, amount, reference and supporting evidence. Failed, cancelled or unconfirmed payment attempts do not create access or a refund entitlement until verified through the applicable provider. Payment disputes may require the payer, institution and provider to cooperate with fraud, chargeback and legal processes."),
            section("Requests and renewal", "Send platform subscription payment enquiries to kiztech01@gmail.com or 08128666531; send institution-controlled fee requests to the relevant institution first. An approved refund is processed through the appropriate channel after verification and may take the time required by the provider and applicable law. Institutions should review renewal choices before the next billing event and keep their payment contacts current."),
        ],
    },
    "acceptable-use": {
        "title": "Acceptable Use Policy",
        "summary": "Standards for safe, lawful and academically responsible use.",
        "acceptance_required": False,
        "sections": [
            section("Authorised educational use", "Use EduConnect only for legitimate educational, administrative and related purposes authorised by the relevant institution. Follow role-based permissions, institutional regulations and applicable law. Do not upload or disclose information unless you have authority to do so."),
            section("Prohibited conduct", "You must not attempt unauthorised access, account impersonation, credential sharing, tenant-isolation bypassing, scraping, probing or service disruption. You must not disclose private student records, forge examination cards, falsify results or payments, make unauthorised academic-record changes, upload malware or harmful files, harass others, or interfere with payment processing."),
            section("Academic integrity and AI", "Do not use the platform or AI features to commit plagiarism, contract cheating, examination misconduct, fraudulent admissions activity or any other academic fraud. AI output must be reviewed by an authorised human and must not be represented as verified institutional advice, a final mark, an approval or evidence without appropriate review."),
            section("Reporting and enforcement", "Report suspected misuse, security concerns or harmful content to the relevant institution or KizTech at kiztech01@gmail.com. KizTech and institutions may investigate proportionately, preserve relevant records, restrict access, correct records, notify affected parties where appropriate and refer conduct to lawful authorities. Enforcement will take account of severity, evidence, risk and applicable obligations."),
        ],
    },
    "cookies": {
        "title": "Cookie Policy",
        "summary": "The essential cookies currently used by this Django application.",
        "acceptance_required": False,
        "sections": [
            section("Cookies currently used", "The current EduConnect application uses Django authentication/session and CSRF-protection cookies, commonly named sessionid and csrftoken. The session cookie supports sign-in and session continuity. The CSRF cookie helps protect submitted forms from cross-site request forgery. The precise cookie attributes may vary between local and production configuration, including secure-cookie settings when production security settings are enabled."),
            section("What is not represented", "This implementation does not represent that analytics, advertising, profiling, social-media tracking, cookie-consent banners or cookie-preference tools are currently installed. If non-essential tracking technologies are introduced, this policy and the relevant user controls should be reviewed before they are presented as implemented."),
            section("Your choices", "Browser settings can limit or remove cookies, but blocking essential session or CSRF cookies can prevent sign-in, payment forms and other protected actions from working. Questions about the application's cookie use may be sent to kiztech01@gmail.com."),
        ],
    },
    "dpa": {
        "title": "Data Processing Agreement (DPA)",
        "summary": "A reviewable institutional agreement template, not a student-facing contract.",
        "acceptance_required": False,
        "sections": [
            section("Purpose and status", "This document is a template for the contractual relationship between KizTech and a participating university. It should be completed, reviewed and signed only by authorised institutional representatives where required. It is not an executed agreement and students, parents, lecturers or ordinary portal users are not required to sign it to use EduConnect."),
            section("Roles and instructions", "For institutional data placed in EduConnect, the university normally acts as controller or equivalent responsible party and KizTech acts as processor or service provider, processing personal data only on documented institutional instructions unless applicable law, including the Nigeria Data Protection Act 2023 where applicable, requires otherwise. The university is responsible for the lawfulness, fairness, transparency and accuracy of the data and instructions it provides."),
            section("Processing details and confidentiality", "Processing may include hosting, storage, retrieval, transmission, support, security, backup and authorised operation of academic, administrative, account, communication, payment-related and technical records. KizTech personnel and authorised service providers should be bound by appropriate confidentiality obligations and access only the information needed for their duties."),
            section("Safeguards, assistance and subprocessors", "The parties should document appropriate technical and organisational safeguards, assistance with data-subject requests and impact assessments, security-incident cooperation, audit arrangements and approved subprocessors in the signed agreement. Where a subprocessor or international transfer is relevant, the university and KizTech should assess the applicable legal requirements and safeguards rather than assuming a standard arrangement."),
            section("Return, deletion and termination", "On termination or a valid written request, institutional data may be returned or deleted subject to law, security, backup, dispute and retention requirements. The signed agreement should specify the processing duration, data categories, affected persons, deletion method, contact points and any transition assistance. This template requires legal and data-protection review before adoption."),
        ],
    },
    "security": {
        "title": "Security and Data Protection Policy",
        "summary": "Implemented safeguards, operational limits and shared security responsibilities.",
        "acceptance_required": False,
        "sections": [
            section("Implemented application controls", "EduConnect implements authentication, role-based authorisation, tenant-aware data handling, CSRF protection, audit logging for selected sensitive actions, controlled document-download checks and payment reference verification workflows. Production settings enable secure cookies, HTTPS redirect and related browser protections when deployed with the required production configuration. These measures reduce risk but do not guarantee absolute security or a certification."),
            section("Data and document handling", "Access to institutional records is intended to be scoped by role and tenant. Uploaded documents and payment-related records are handled through application permissions; users must upload only authorised, lawful content. Secrets such as payment keys are configured outside the repository and should never be posted in messages, documents or support requests."),
            section("Monitoring, vulnerabilities and backups", "The service records selected audit and operational events. KizTech should assess, prioritise and remediate reported vulnerabilities using a risk-based process. Backup, recovery, hosting monitoring and deployment arrangements depend on the environment and are not represented here as a guaranteed recovery objective, retention period or independently certified control unless verified in a written agreement."),
            section("AI and incident safeguards", "AI assistance is role-scoped and should not replace human review of consequential decisions. Users must avoid submitting unnecessary sensitive information and must validate AI outputs. Suspected compromise, unauthorised disclosure or security vulnerability should be reported promptly to the institution and kiztech01@gmail.com. KizTech will investigate and coordinate incident communication as required by law or contract."),
            section("Institutional responsibilities", "Institutions must assign roles carefully, remove access when it is no longer needed, train authorised users, protect local devices and processes, and maintain their own academic, records-management and incident procedures. Recommended improvements should be assessed against the institution's environment; they are not claims that every control is already implemented."),
        ],
    },
    "sla": {
        "title": "Service Availability and Service Level Agreement (SLA)",
        "summary": "Service scope, maintenance and contractual availability expectations.",
        "acceptance_required": False,
        "sections": [
            section("Service scope", "EduConnect provides the features enabled for an institution's subscription and configuration. The service may depend on hosting, network, email, payment, AI and other third-party services. This policy does not promise a specific uptime percentage, response time, recovery objective, support hour or service credit."),
            section("Maintenance and interruptions", "KizTech may perform planned or emergency maintenance, security work, upgrades or configuration changes. Availability can also be affected by institutional configuration, internet connectivity, third-party outages, payment-provider responses, force majeure and events outside reasonable control. Where practical, relevant institutions will receive appropriate communications through agreed channels."),
            section("Support, incidents and suspension", "Support questions may be directed to kiztech01@gmail.com or 08128666531. KizTech will assess reported incidents according to their impact and available information, but this policy does not create an unverified response-time commitment. Access may be limited or suspended to protect security, comply with law, address misuse or manage subscription status."),
            section("Institutional agreement", "Any binding service levels, support contacts, maintenance windows, incident escalation, data-return commitments, service credits or exclusions must be stated in the applicable signed institutional contract or SLA. That agreement may supplement this global policy but should not silently replace a published global version for other institutions."),
        ],
    },
}
