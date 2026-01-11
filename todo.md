- test real booking scenarios.
- make the the final changes. house rules in hte booking process to agree on.

- Have 5 different css files.
schema
security
- Get it in Arabic.

- Get domeain name. check LibyanHolidays.com
- make classic site.

- Get ec2 for testing. 

- future work:
 - otp for booking
 - limit visitors
 - integrate with facebook messenger
 - direct payment.



Libya 2222Nagwa

in the Tape Chart tab, add new box like the Arrival Today, etc, that says New Requests. that is the number of pending bookings.

in the finianical tab, remove futrue deposite and make the outstanding for the next 7 days, and the revenue in the past 7 days and change the recent  transactions to only today transaction.

in the reservaitons tab, make reservaiton for confirmed and checkin only.


Make new tab called New Requests. this should have table of database similar to the one in the Reservations tab but has only the Pending and cancelled bookings.

- 

- remove Reset Staff password.
 remove staff login,
 

new checks:
remove guest filter
put max booked rooms to two
one request for the next 10 months. make the change in the js side.

the tap chart is overflowing cauing change in the background color when the number of rooms increase. can you fix that. also, can you make any kind of seraparator between room types? let say I have 5 rooms of Single room and 3 rooms of King room, there should be separation or gap between these two in types. second, in the finanial tab, I don't see the transaction or 30 day revenue. thrid, I want to move the Active Guests section to separate tab just for that. Lastly, I want to use pagination in the followign tabs, booking in All Upcoming Reservations, in the new Active Guests tab, Visitor tab, logs tab. use 50 per page


is this gonna work iwth https?
remove admin password hash in the site config table
booking, booked rooms ??
I see datetime.utcnow()

add new featrues to hotel, logest booking days, max rooms, and make the limiter selectable in the settings page of the amin page. @router.post("/app/{extension}/search") adn @router.post("/app/{extension}/book/confirm"). also, change the limit to be function of the ip and the externsion as well. so i want tthe limit to be separate for each hotel page.