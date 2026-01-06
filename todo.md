- test real booking scenarios.
- make the the final changes. house rules in hte booking process to agree on.

- Have 5 different css files.
schema
security
all time in libya time
- Get it in Arabic.

- Get domeain name. check LibyanHolidays.com
- make classic site.

- Get ec2 for testing. 

- future work:
 - otp for booking
 - limit visitors
 - integrate with facebook messenger
 - direct payment.



v29:

server {
    listen 80;
    server_name yourdomain.com;

    client_max_body_size 10M;  # <--- CRITICAL: Allow up to 10MB uploads

    location / {
        proxy_pass http://127.0.0.1:8000;
        proxy_set_header Host $host;
        proxy_set_header X-Real-IP $remote_addr;
    }
}

- 

- remove Reset Staff password.
- multi line in the succes message?
 - email and phone not saved.
 remove staff login,
 change to 2026