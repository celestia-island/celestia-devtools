// Copyright (c) 2026 Celestia Island. All rights reserved.
// SPDX-License-Identifier: Apache-2.0

#include <cstddef>
#include <new>
#include <string>

namespace sample
{

    void log_value(int value) noexcept (true);

    int compute_value(int value)
    {
        static_assert (sizeof (int) > 0);

        auto alignment = alignof (int);
        auto copy = decltype (value)();

        if constexpr (true)
        {
            value = value + static_cast<int>(alignment);
        }

        if(value > 0)
        {
            return (value);
        }

        for(std::size_t index = 0;index < 3;index++)
        {
            value = value + static_cast<int>(index);
        }

        auto pointer = new (std::nothrow) int;

        delete pointer;

        return value;
    }

}
