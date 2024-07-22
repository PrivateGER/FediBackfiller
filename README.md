Basic HTTP server that listens for requests pointing to remote posts, fetches their context and manually imports the replies received from the remote API by requesting the AP URI.

This is pretty bad. It calls the AP fetcher API from the outside. 

It SHOULD be integrated into Misskey. I do not want to integrate it into Misskey.