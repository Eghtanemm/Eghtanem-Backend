<?php

namespace App;

enum SocialAuthenticationProvider: int
{
    case GOOGLE = 1;
    case APPLE = 2;
}
