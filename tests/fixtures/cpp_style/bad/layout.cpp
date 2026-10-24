// Copyright (c) 2026 Celestia Island. All rights reserved.

#include <cstddef>

void update_value(int value)
{
    if (value > 0) return;

    for (std::size_t index = 0; index < 3; index++)
    {
        value = value + 1;
    }

    if (value == 0)
    {
        return;
    }
    else value = 1;

    /* legacy helper */
    int first = 0; int second = 1;
}
