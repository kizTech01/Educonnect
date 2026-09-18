# Hosting EduConnect on Render

1. Push this project to a private GitHub repository. Do not commit `.env` or SMTP passwords.
2. Create a new **Blueprint** on Render and select the repository. Render will read `render.yaml` and create the PostgreSQL database and web service.
3. In the Blueprint setup screen, supply the requested SMTP values for the web service:

   - `EMAIL_HOST`
   - `EMAIL_HOST_USER`
   - `EMAIL_HOST_PASSWORD`
   - `DEFAULT_FROM_EMAIL`
   - `BOOTSTRAP_ADMIN_USERNAME`
   - `BOOTSTRAP_ADMIN_PASSWORD`

   `BOOTSTRAP_ADMIN_EMAIL` is optional. Use a strong, unique bootstrap password.

   Use a transactional email provider and verify the sender domain with that provider before going live.
4. Deploy. The web service applies migrations before deploy.
5. Set `CORE_DOMAIN` to the bare platform hostname, for example `educonnect.example.edu.ng`, and redeploy. Django derives both the core host and its institution subdomains, plus their HTTPS CSRF origins, from this one value.
6. Test password-reset and lecturer-message emails. When you are ready to pay for automated class reminders, add a Render cron service that runs `python manage.py send_due_course_reminders` every minute.
7. Before accepting payments, have each department's HOD enter that department's Paystack public and secret keys in **Departmental → API and Document**. Gateways intentionally start unconfigured; no payment key is stored in the repository.

For the Docker/Caddy deployment, start by copying `.env.production.example` to
`.env.production`, then replace every placeholder before starting the stack.

## Important production notes

- The application uses PostgreSQL through `DATABASE_URL` in production. Do not use SQLite on Render.
- Class reminders are disabled in this deployment blueprint. When a cron service is added later, delivery uses private BCC batching and retries failed SMTP batches until the class begins.
- Static files are served by WhiteNoise. The Render blueprint mounts a persistent disk at the application's media directory so uploaded course files, passport photos, and documents survive deploys. Back up this disk regularly; use object storage if you need multi-region or multi-instance media access.
- Browser alerts require HTTPS, browser permission, and an open EduConnect page. Email remains the reliable notification channel when the browser is closed.
